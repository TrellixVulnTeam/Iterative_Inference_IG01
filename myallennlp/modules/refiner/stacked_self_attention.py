from overrides import overrides
import torch
from torch.nn import Dropout
import torch.nn.functional as F

import copy
from allennlp.modules.feedforward import FeedForward
from allennlp.modules.layer_norm import LayerNorm
from allennlp.modules.seq2seq_encoders.seq2seq_encoder import Seq2SeqEncoder
from allennlp.nn.activations import Activation
from allennlp.nn import InitializerApplicator, RegularizerApplicator

from allennlp.modules.matrix_attention.bilinear_matrix_attention import BilinearMatrixAttention

from allennlp.nn.util import masked_softmax, weighted_sum
from allennlp.nn.util import add_positional_features
from typing import List
from myallennlp.modules.relaxed_gcn import RelaxedGCN
from myallennlp.modules.reparametrization.gumbel_softmax import masked_gumbel_softmax

@Seq2SeqEncoder.register("stacked_self_attention_refiner")
class StackedSelfAttentionRefinment(Seq2SeqEncoder):
    # pylint: disable=line-too-long
    """
    Implements a stacked self-attention encoder similar to the Transformer
    architecture in `Attention is all you Need
    <https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/0737da0767d77606169cbf4187b83e1ab62f6077>`_ .

    This encoder combines 3 layers in a 'block':

    1. A 2 layer FeedForward network.
    2. Multi-headed self attention, which uses 2 learnt linear projections
       to perform a dot-product similarity between every pair of elements
       scaled by the square root of the sequence length.
    3. Layer Normalisation.

    These are then stacked into ``num_layers`` layers.

    Parameters
    ----------
    input_dim : ``int``, required.
        The input dimension of the encoder.
    hidden_dim : ``int``, required.
        The hidden dimension used for the _input_ to self attention layers
        and the _output_ from the feedforward layers.
    projection_dim : ``int``, required.
        The dimension of the linear projections for the self-attention layers.
    feedforward_hidden_dim : ``int``, required.
        The middle dimension of the FeedForward network. The input and output
        dimensions are fixed to ensure sizes match up for the self attention layers.
    num_layers : ``int``, required.
        The number of stacked self attention -> feedfoward -> layer normalisation blocks.
    num_attention_heads : ``int``, required.
        The number of attention heads to use per layer.
    use_positional_encoding: ``bool``, optional, (default = True)
        Whether to add sinusoidal frequencies to the input tensor. This is strongly recommended,
        as without this feature, the self attention layers have no idea of absolute or relative
        position (as they are just computing pairwise similarity between vectors of elements),
        which can be important features for many tasks.
    dropout_prob : ``float``, optional, (default = 0.1)
        The dropout probability for the feedforward network.
    residual_dropout_prob : ``float``, optional, (default = 0.2)
        The dropout probability for the residual connections.
    attention_dropout_prob : ``float``, optional, (default = 0.1)
        The dropout probability for the attention distributions in each attention layer.
    """
    def __init__(self,
                 input_dim: int,
                 hidden_dim: int,
                 projection_dim: int,
                 feedforward_hidden_dim: int,
                 num_layers: int,
                 num_attention_heads: int,
                 use_positional_encoding: bool = True,
                 dropout_prob: float = 0.1,
                 residual_dropout_prob: float = 0.2,
                 attention_dropout_prob: float = 0.1,
                 gumbel_head_t:float = 1.0,
                 gumbel_at_test:bool=False,
                 user_new_attention:bool=False) -> None:
        super(StackedSelfAttentionRefinment, self).__init__()
        self._gumbel_at_test = gumbel_at_test
        self._use_positional_encoding = use_positional_encoding
        self._gcn_layers: List[RelaxedGCN] = []
        self._feedfoward_layers: List[FeedForward] = []
        self._layer_norm_layers: List[LayerNorm] = []
        self._feed_forward_layer_norm_layers: List[LayerNorm] = []
        self._user_new_attention = user_new_attention
        self._num_attention_heads = num_attention_heads
        self._num_layers = num_layers
        feedfoward_input_dim = input_dim
        self.gumbel_head_t = gumbel_head_t

        self.head_arc_feedforward = FeedForward(hidden_dim, 1,
                                                projection_dim,
                                                    Activation.by_name("elu")())
        self.child_arc_feedforward = copy.deepcopy(self.head_arc_feedforward)

        self.arc_attention = BilinearMatrixAttention(projection_dim,
                                                     projection_dim,
                                                     use_input_biases=True)

        for i in range(num_layers):
            feedfoward = FeedForward(feedfoward_input_dim,
                                     activations=[Activation.by_name('relu')(),
                                                  Activation.by_name('linear')()],
                                     hidden_dims=[feedforward_hidden_dim, hidden_dim],
                                     num_layers=2,
                                     dropout=dropout_prob)

            self.add_module(f"feedforward_{i}", feedfoward)
            self._feedfoward_layers.append(feedfoward)

            feedforward_layer_norm = LayerNorm(feedfoward.get_output_dim())
            self.add_module(f"feedforward_layer_norm_{i}", feedforward_layer_norm)
            self._feed_forward_layer_norm_layers.append(feedforward_layer_norm)

            gcn = RelaxedGCN(num_heads=num_attention_heads,
                                                    input_dim=hidden_dim,
                                                    values_dim=hidden_dim,
                             attention_dropout_prob = attention_dropout_prob)
            self.add_module(f"self_attention_{i}", gcn)
            self._gcn_layers.append(gcn)

            layer_norm = LayerNorm(gcn.get_output_dim())
            self.add_module(f"layer_norm_{i}", layer_norm)
            self._layer_norm_layers.append(layer_norm)

            feedfoward_input_dim = hidden_dim

        self.dropout = Dropout(residual_dropout_prob)
        self._input_dim = input_dim
        self._output_dim = self._gcn_layers[-1].get_output_dim()

    @overrides
    def get_input_dim(self) -> int:
        return self._input_dim

    @overrides
    def get_output_dim(self) -> int:
        return self._output_dim

    @overrides
    def is_bidirectional(self):
        return False

    @overrides
    def forward(self, inputs: torch.Tensor,
                input_attended_arcs:torch.Tensor,
                mask: torch.Tensor,
                num_iterations:int = 1): # pylint: disable=arguments-differ
        output_attended_arcs_list = [input_attended_arcs] #input_attended_arcs
        for k in range(num_iterations):
            input_attended_arcs = input_attended_arcs.detach()
            inputs = inputs.detach()
            if self._use_positional_encoding:
                output = add_positional_features(inputs)
            else:
                output = inputs
            for i,(gcn,
                 feedforward,
                 feedforward_layer_norm,
                 layer_norm) in enumerate(zip(self._gcn_layers,
                                    self._feedfoward_layers,
                                    self._feed_forward_layer_norm_layers,
                                    self._layer_norm_layers)):
                cached_input = output
                # Project output of attention encoder through a feedforward
                # network and back to the input size for the next layer.
                # shape (batch_size, timesteps, input_size)
                feedforward_output = feedforward(output)
                feedforward_output = self.dropout(feedforward_output)
                if feedforward_output.size() == cached_input.size():
                    # First layer might have the wrong size for highway
                    # layers, so we exclude it here.
                    feedforward_output = feedforward_layer_norm(feedforward_output + cached_input)

                # shape (batch_size, sequence_length, sequence_length)
                if self.training or self._gumbel_at_test :
                    attention = masked_gumbel_softmax(input_attended_arcs, tau=self.gumbel_head_t, mask=mask)
                else:
                    attention = masked_softmax(input_attended_arcs, mask=mask)

                if self._num_attention_heads == 2:
                # shape (batch_size, 1, sequence_length, sequence_length)
                    attention = attention.unsqueeze(1)
                    anti_attention = attention.transpose(2,3)
                    multi_head_attention = torch.cat([attention,anti_attention],dim=1)
                # shape (batch_size, sequence_length, hidden_dim)
                attention_output = gcn(feedforward_output, multi_head_attention,mask)



                output = layer_norm(self.dropout(attention_output) + feedforward_output)

                if self._user_new_attention or i == self._num_layers -1:
                    head = self.dropout(self.head_arc_feedforward(output))
                    child = self.dropout(self.child_arc_feedforward(output))
                    output_attended_arcs = self.arc_attention(head,child)
                    input_attended_arcs = output_attended_arcs
            output_attended_arcs_list.append(output_attended_arcs)

        return output_attended_arcs_list
