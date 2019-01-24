from overrides import overrides
import torch
from torch.nn import Dropout
import torch.nn.functional as F

import copy
from allennlp.modules.feedforward import FeedForward
from allennlp.modules.layer_norm import LayerNorm
from allennlp.modules import InputVariationalDropout, Embedding
from allennlp.modules.seq2seq_encoders.seq2seq_encoder import Seq2SeqEncoder
from allennlp.nn.activations import Activation
from allennlp.nn import InitializerApplicator, RegularizerApplicator

from allennlp.nn.util import get_device_of, masked_log_softmax, get_lengths_from_binary_sequence_mask, masked_softmax
from allennlp.modules.matrix_attention.bilinear_matrix_attention import BilinearMatrixAttention
import numpy
from allennlp.nn.util import masked_softmax, weighted_sum
from allennlp.nn.util import add_positional_features
from typing import List, Tuple, Dict
from myallennlp.modules.refiner.mlp_forward_backward import MLPForwardBackward
from myallennlp.modules.refiner.graph_auto_encoder import GraphAutoEncoder
from myallennlp.modules.reparametrization.gumbel_softmax import hard, _sample_gumbel, inplace_masked_gumbel_softmax


@Seq2SeqEncoder.register("srl_score_refiner")
class SRLScoreBasedRefiner(Seq2SeqEncoder):
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
                 iterations: int = 1,
                 dropout: float = 0.3,
                 stright_through: bool = False,
                 denoise: bool = False,
                 gradient_as_logits:bool=True,
                 global_gating:bool=False,
                 detach:bool = False,
                 gating:bool = False,
                 hidden_dim: int = 300,
                 node_dim:int = 40,
                 score_dim: int = 40,
                 corrupt_with_output: bool = False,
                 corruption_rate: float = 0.1,
                 corruption_iterations: int = 1,
                 testing_iterations: int = 5,
                 gumbel_t: float = 1.0) -> None:
        super(SRLScoreBasedRefiner, self).__init__()
        self.gumbel_t = gumbel_t
        self.node_dim = node_dim
        self.gradient_as_logits = gradient_as_logits
        self.detach = detach
        self.denoise = denoise
        self.gating = gating
        self.global_gating = global_gating
        self.stright_through = stright_through
        self.iterations = iterations
        self.score_dim = score_dim
        self.hidden_dim = hidden_dim
        self._dropout = Dropout(dropout)
        self.testing_iterations = testing_iterations
        self.dropout = dropout
        self.corruption_rate = corruption_rate
        self._corrupt_mask = lambda x : torch.bernoulli(x.data.new(x.data.size()[:-1]).fill_(1 - self.corruption_rate)).unsqueeze(-1)
        self.corrupt_with_output = corrupt_with_output
        self.corruption_iterations = corruption_iterations

    def set_scorer(self, n_tags: int,input_node_dim:int):
        self.graph_scorer = GraphAutoEncoder(input_node_dim=input_node_dim,node_dim=self.node_dim, edge_dim=n_tags + 1, hidden_dim=self.hidden_dim,
                                             score_dim=self.score_dim, dropout=self.dropout)

    @overrides
    def get_input_dim(self) -> int:
        return self._input_dim

    @overrides
    def get_output_dim(self) -> int:
        return self._output_dim

    @overrides
    def is_bidirectional(self):
        return True

    def one_iteration(self,
            predicate_representation,
            sense_logits,
            extra_representation,
            arc_tag_logits,
            embedded_candidate_preds,
            graph_mask,
            sense_mask,
            predicate_mask,
            soft_tags,
            soft_index,
            arc_tag_probs = None,
            sense_probs = None):

        def get_loss_grad(tag, logits,probs = None):
            if tag is not None and self.training and False:
                if probs is not None: return probs-tag
                return torch.nn.functional.softmax(logits, dim=-1) - tag
            else:
                return 0

        arc_tag_logits = arc_tag_logits  + get_loss_grad(soft_tags, arc_tag_logits,arc_tag_probs)

        if self.denoise and (self.training or (self.gating and arc_tag_probs is not None))  :
            arc_tag_logits = arc_tag_logits  + _sample_gumbel(arc_tag_logits.size(),  out=arc_tag_logits.new())
            arc_tag_probs_soft = torch.nn.functional.softmax(arc_tag_logits , dim=-1)
        else:
            arc_tag_probs_soft = torch.nn.functional.softmax(arc_tag_logits , dim=-1)

        arc_tag_probs = hard(arc_tag_probs_soft, graph_mask)  if self.stright_through else arc_tag_probs_soft

        # (batch_size, predicates_len, max_senses )
        sense_logits = sense_logits  + get_loss_grad(soft_index, sense_logits,sense_probs)

        if self.denoise and self.training:
            sense_logits = sense_logits  + _sample_gumbel(sense_logits.size(),out=sense_logits.new())

            sense_probs_soft = torch.nn.functional.softmax(sense_logits  , dim=-1)
        else:
            sense_probs_soft = torch.nn.functional.softmax(sense_logits, dim=-1)


    #    print ("sense_probs_soft",sense_probs_soft.size())
    #    print ("sense_mask",sense_mask.size())
        sense_probs = hard(sense_probs_soft, sense_mask) if self.stright_through else sense_probs_soft
        # (batch_size, predicates_len, node_dim)

        predicate_emb = (sense_probs.unsqueeze(2).matmul(embedded_candidate_preds)).squeeze(2) * predicate_mask.unsqueeze(-1) + predicate_representation

        scores, grad_to_predicate_emb, grad_to_arc_tag_probs = self.graph_scorer(predicate_emb,
                                                                                 extra_representation,
                                                                                 arc_tag_probs*graph_mask, graph_mask)

        grad_to_sense_probs = embedded_candidate_preds.matmul(grad_to_predicate_emb.unsqueeze(-1)).squeeze(-1)
        return arc_tag_logits, arc_tag_probs_soft, sense_logits, sense_probs_soft,  scores, grad_to_sense_probs, grad_to_arc_tag_probs

    def gold_feed(self, soft_tags,
                      soft_index,
                    predicate_representation,
                        extra_node_representation,
                      embedded_candidate_preds,
                        graph_mask,
                        sense_mask,
        predicate_mask):


        # (batch_size, sequence_length, node_dim)
        predicate_emb = (soft_index.unsqueeze(2).matmul(embedded_candidate_preds)).squeeze(
            2)* predicate_mask.unsqueeze(-1) + predicate_representation
        scores, grad_to_predicate_emb, grad_to_arc_tag_probs = self.graph_scorer(predicate_emb,
                                                                                 extra_node_representation,
                                                                                 soft_tags*graph_mask, graph_mask)

        grad_to_sense_probs = embedded_candidate_preds.matmul(grad_to_predicate_emb.unsqueeze(-1)).squeeze(-1)
        return scores, grad_to_sense_probs, grad_to_arc_tag_probs

    @overrides
    def forward(self,predicate_representation,
                                extra_representation,
                                input_arc_tag_logits,
                                input_sense_logits,
                                embedded_candidate_preds,
                                graph_mask,
                                sense_mask,
                predicate_mask,
                                soft_tags=None,
                                soft_index=None):  # pylint: disable=arguments-differ
        '''

        :param predict_representation:
        :param argument_representation:
        :param input_arc_tag_logits:
        :param arc_tag_probs:
        :param mask:
            A mask of shape (batch_size, sequence_length), denoting unpadded
            elements in the sequence.
        :param arc_tags:
        :param pre_dropout_mask:
        :param embedded_candidate_preds: (batch_size, sequence_length, max_senses, node_dim)
        :return:
        '''
        # shape (batch_size, predicates_len, batch_max_senses )  sense_mask


        arc_tag_logits, arc_tag_probs, sense_logits, sense_probs, scores, grad_to_sense_probs, grad_to_arc_tag_probs = self.one_iteration(
            predicate_representation,
            input_sense_logits,
            extra_representation,
            input_arc_tag_logits,
            embedded_candidate_preds,
            graph_mask,
            sense_mask,
            predicate_mask,
            soft_tags,
            soft_index,
        )

        scores = scores + (arc_tag_probs * input_arc_tag_logits).sum(-1, keepdim=True)/scores.size(-1)*graph_mask
        arc_tag_logits_list = [arc_tag_logits]
        arc_tag_probs_list = [arc_tag_probs]
        sense_logits_list = [sense_logits]
        sense_probs_list = [sense_probs]
        scores_list = [scores]
        grad_to_arc_tag_probs_list = [grad_to_arc_tag_probs]
        iterations = self.iterations if self.training else self.testing_iterations
        #      arg_intermediates_list = []
        if self.detach:
            input_sense_logits = input_sense_logits.detach()
            input_arc_tag_logits = input_arc_tag_logits.detach()
          #  predicate_representation = predicate_representation.detach()
        for i in range(iterations):

            # (batch_size, sequence_length, max_senses )
            sense_logits = self._dropout (input_sense_logits) + grad_to_sense_probs

            arc_tag_logits = self._dropout (input_arc_tag_logits) + grad_to_arc_tag_probs

            arc_tag_logits, arc_tag_probs, sense_logits, sense_probs,  scores, grad_to_sense_probs, grad_to_arc_tag_probs = self.one_iteration(
                predicate_representation,
                sense_logits,
                extra_representation,
                arc_tag_logits,
                embedded_candidate_preds,
                graph_mask,
                sense_mask,
                predicate_mask,
                soft_tags,
                soft_index,
                arc_tag_probs,
                sense_probs
            )
            scores = scores + (arc_tag_probs * input_arc_tag_logits).sum(-1, keepdim=True)/scores.size(-1)*graph_mask
            if self.gating and not self.training:
                if self.global_gating:
                    update_mask = ((scores-scores_list[-1]).sum(3,keepdim=True).sum(2,keepdim=True)  > 0).float()
                else:
                    update_mask = ((scores-scores_list[-1]).sum(-1,keepdim=True) > 0).float()
                old_mask = 1 - update_mask
                grad_to_arc_tag_probs = grad_to_arc_tag_probs * update_mask + grad_to_arc_tag_probs_list[-1]* old_mask
                arc_tag_probs = arc_tag_probs * update_mask + arc_tag_probs_list[-1]* old_mask
                arc_tag_logits = arc_tag_logits * update_mask + arc_tag_logits_list[-1]* old_mask

            if self.gradient_as_logits:
                arc_tag_logits_list.append(grad_to_arc_tag_probs)
                sense_logits_list.append(grad_to_sense_probs)
            else:
                arc_tag_logits_list.append(arc_tag_logits)
                sense_logits_list.append(sense_logits)
            grad_to_arc_tag_probs_list.append(grad_to_arc_tag_probs)
            arc_tag_probs_list.append(arc_tag_probs)
            sense_probs_list.append(sense_probs)
            scores_list.append(scores)

        c_arc_tag_logits_list = []
        c_arc_tag_probs_list = []
        c_sense_logits_list = []
        c_sense_probs_list = []
        c_scores_list = []

        if soft_tags is not None :


            gold_scores, grad_to_sense_probs, grad_to_arc_tag_probs = self.gold_feed( soft_tags,
                      soft_index,
                    predicate_representation,
                        extra_representation,
                      embedded_candidate_preds,
                        graph_mask,
                        sense_mask,
                                                                                      predicate_mask)

            gold_scores = gold_scores + (soft_tags * input_arc_tag_logits).sum(-1, keepdim=True)/scores.size(-1)*graph_mask
            gold_sense_logits = self._dropout (input_sense_logits)  + grad_to_sense_probs
            gold_arc_tag_logits = self._dropout (input_arc_tag_logits)  + grad_to_arc_tag_probs

            gold_arc_tag_probs = torch.nn.functional.softmax(gold_arc_tag_logits, dim=-1)
            assert gold_arc_tag_probs.dim() == 4, gold_arc_tag_probs.size()
            gold_sense_probs = torch.nn.functional.softmax(gold_sense_logits, dim=-1)
            if self.gradient_as_logits:
                gold_results = [gold_scores, grad_to_sense_probs , gold_sense_probs, grad_to_arc_tag_probs,gold_arc_tag_probs]
            else:
                gold_results = [gold_scores, gold_sense_logits , gold_sense_probs, gold_arc_tag_logits,gold_arc_tag_probs]
        else:
            gold_results = [None,None,None,None,None]


        iterations = self.corruption_iterations if self.training else 1
        if self.corruption_rate and soft_tags is not None and self.training:
            for i in range(iterations):
                if self.corrupt_with_output:
                    soft_tags = self.corrupt_one_hot(soft_tags,graph_mask)
                    soft_index = self.corrupt_index(soft_index,sense_mask)
                else:
                    soft_tags = self.corrupt_one_hot(soft_tags,graph_mask,arc_tag_probs_list[-1])
                    soft_index = self.corrupt_index(soft_index,sense_mask,sense_probs_list[-1])
                gold_scores, grad_to_sense_probs, grad_to_arc_tag_probs = self.gold_feed( soft_tags,
                          soft_index,
                        predicate_representation,
                            extra_representation,
                          embedded_candidate_preds,
                            graph_mask,
                            sense_mask,
                            predicate_mask)

                gold_scores = gold_scores + (soft_tags * input_arc_tag_logits).sum(-1, keepdim=True)/gold_scores.size(-1)*graph_mask

                gold_sense_logits = self._dropout (input_sense_logits)  + grad_to_sense_probs
                gold_arc_tag_logits = self._dropout (input_arc_tag_logits)  + grad_to_arc_tag_probs

                gold_arc_tag_probs = torch.nn.functional.softmax(gold_arc_tag_logits, dim=-1)

                gold_sense_probs = torch.nn.functional.softmax(gold_sense_logits, dim=-1)

                if self.gradient_as_logits:
                    c_arc_tag_logits_list.append(grad_to_arc_tag_probs)
                    c_sense_logits_list.append(grad_to_sense_probs)
                else:
                    c_arc_tag_logits_list.append(gold_arc_tag_logits)
                    c_sense_logits_list.append(gold_sense_logits)
                c_arc_tag_probs_list.append(gold_arc_tag_probs)
                c_sense_probs_list.append(gold_sense_probs)
                c_scores_list.append(gold_scores)

        return (arc_tag_logits_list, arc_tag_probs_list, sense_logits_list, sense_probs_list, scores_list ),\
               (c_arc_tag_logits_list, c_arc_tag_probs_list, c_sense_logits_list, c_sense_probs_list, c_scores_list ), gold_results


    def corrupt_one_hot(self,gold,mask,sample = None):
        corrupt_mask = self._corrupt_mask(mask)
        if sample is None:
            sample = torch.distributions.one_hot_categorical.OneHotCategorical(logits=torch.zeros_like(gold)).sample()


        return mask * (gold*corrupt_mask+(1-corrupt_mask)*sample)

    def corrupt_index(self,soft_index,sense_mask,sample = None):
        sense_mask = sense_mask +1e-6
        mask_sum =  sense_mask.sum(dim=-1,keepdim=True)


        #batch pre_len 1
        corrupt_mask = self._corrupt_mask(mask_sum)

        if sample is None:
            probs = sense_mask / mask_sum
            sample = torch.distributions.one_hot_categorical.OneHotCategorical(probs=probs).sample()

        out = sense_mask * (soft_index*corrupt_mask+(1-corrupt_mask)*sample)
        ''' 
        print ("soft_index",soft_index.size())
        print ("sense_mask",sense_mask.size())
        print ("mask_sum",mask_sum.size())
        print ("probs",probs.size())
        print ("corrupt_mask",corrupt_mask.size())
        print ("random_sample",random_sample.size())
        print ("out",out.size()) '''

        return out