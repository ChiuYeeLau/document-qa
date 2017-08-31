from typing import List, Optional, Union
import numpy as np
import tensorflow as tf
from tensorflow import Tensor
from tensorflow.contrib.layers import fully_connected

from configurable import Configurable
from model import Prediction
from nn.layers import SequenceBiMapper, MergeLayer, Mapper, get_keras_initialization, SequenceMapper, SequenceEncoder, \
    FixedMergeLayer, AttentionPredictionLayer, SequencePredictionLayer, SequenceMultiEncoder
from nn.ops import VERY_NEGATIVE_NUMBER, exp_mask, segment_logsumexp
from nn.span_prediction_ops import best_span_from_bounds, to_unpacked_coordinates, \
    to_packed_coordinates, packed_span_f1_mask


class BoundaryPrediction(Prediction):
    """ Individual logits for the span start/end """
    def __init__(self, start_prob, end_prob,
                 start_logits, end_logits):
        self.start_probs = start_prob
        self.end_probs = end_prob
        self.start_logits = start_logits
        self.end_logits = end_logits
        self._bound_predictions = {}

    def get_best_span(self, bound: int):
        if bound in self._bound_predictions:
            return self._bound_predictions[bound]
        else:
            pred = best_span_from_bounds(self.start_logits, self.end_logits, bound)
            self._bound_predictions[bound] = pred
            return pred

    def get_span_scores(self):
        return tf.exp(tf.expand_dims(self.start_logits, 2) + tf.expand_dims(self.end_logits, 1))


class PackedSpanPrediction(Prediction):
    """ Logits for each span in packed format (batch, packed_coordinate) """
    def __init__(self, logits, l, bound):
        self.bound = bound
        self.logits = logits
        argmax = tf.argmax(logits, axis=1)
        self.best_score = tf.reduce_max(logits, axis=1)
        self.predicted_span = to_unpacked_coordinates(argmax, l, bound)
        self.l = l

    def get_best_span(self, bound):
        if bound > self.bound:
            raise ValueError()
        if bound < self.bound:
            cutoff = self.l * bound - bound * (bound - 1) // 2
            logits = self.logits[:, :cutoff]
            argmax = tf.argmax(logits, axis=1)
            best_score = tf.reduce_max(logits, axis=1)
            predicted_span = to_unpacked_coordinates(argmax, self.l, bound)
            return predicted_span, best_score

        return self.predicted_span, self.best_score


class ConfidencePrediction(Prediction):
    """ boundary logits with an additional confidence logit """
    def __init__(self, span_probs,
                 start_logits, end_logits,
                 none_prob, non_op_logit):
        self.span_probs = span_probs
        self.none_prob = none_prob
        self.start_logits = start_logits
        self.end_logits = end_logits
        self.non_op_logit = non_op_logit

    def get_best_span(self, bound: int):
        return best_span_from_bounds(self.start_logits, self.end_logits, bound)

    def get_span_scores(self):
        return tf.exp(tf.expand_dims(self.start_logits, 2) + tf.expand_dims(self.end_logits, 1))


class SpanFromBoundsPredictor(Configurable):
    def predict(self, answer, start_logits, end_logits, mask) -> Prediction:
        raise NotImplementedError()


class IndependentBounds(SpanFromBoundsPredictor):
    def __init__(self, aggregate="sum"):
        self.aggregate = aggregate

    def predict(self, answer, start_logits, end_logits, mask) -> Prediction:
        masked_start_logits = exp_mask(start_logits, mask)
        masked_end_logits = exp_mask(end_logits, mask)

        if len(answer) == 1:
            # answer span is encoding in a sparse int array
            answer_spans = answer[0]
            losses1 = tf.nn.sparse_softmax_cross_entropy_with_logits(
                logits=masked_start_logits, labels=answer_spans[:, 0])
            losses2 = tf.nn.sparse_softmax_cross_entropy_with_logits(
                logits=masked_end_logits, labels=answer_spans[:, 1])
            loss = tf.add_n([tf.reduce_mean(losses1), tf.reduce_mean(losses2)], name="loss")
        elif len(answer) == 2 and all(x.dtype == tf.bool for x in answer):
            # all correct start/end bounds are marked in a dense bool array
            # In this case there might be multiple answer spans, so we need an aggregation strategy
            losses = []
            for answer_mask, logits in zip(answer, [masked_start_logits, masked_end_logits]):
                log_norm = tf.reduce_logsumexp(logits, axis=1)
                if self.aggregate == "sum":
                    log_score = tf.reduce_logsumexp(logits +
                                                    VERY_NEGATIVE_NUMBER * (1 - tf.cast(answer_mask, tf.float32)),
                                                    axis=1)
                elif self.aggregate == "max":
                    log_score = tf.reduce_max(logits +
                                              VERY_NEGATIVE_NUMBER * (1 - tf.cast(answer_mask, tf.float32)), axis=1)
                else:
                    raise ValueError()
                losses.append(tf.reduce_mean(-(log_score - log_norm)))
            loss = tf.add_n(losses)
        else:
            raise NotImplemented()
        tf.add_to_collection(tf.GraphKeys.LOSSES, loss)
        return BoundaryPrediction(tf.nn.softmax(masked_start_logits),
                                  tf.nn.softmax(masked_end_logits),
                                  masked_start_logits, masked_end_logits)


class IndependentBoundsGrouped(SpanFromBoundsPredictor):
    def __init__(self, aggregate="sum"):
        self.aggregate = aggregate

    def predict(self, answer, start_logits, end_logits, mask) -> Prediction:
        masked_start_logits = exp_mask(start_logits, mask)
        masked_end_logits = exp_mask(end_logits, mask)

        if len(answer) == 3:
            group_ids = answer[2]
            # Turn the ids into segment ids using tf.unique
            _, group_segments = tf.unique(group_ids, out_idx=tf.int32)

            losses = []
            for answer_mask, logits in zip(answer, [masked_start_logits, masked_end_logits]):
                group_norms = segment_logsumexp(logits, group_segments)
                if self.aggregate == "sum":
                    log_score = segment_logsumexp(logits + VERY_NEGATIVE_NUMBER * (1 - tf.cast(answer_mask, tf.float32)),
                                                  group_segments)
                else:
                    raise ValueError()
                losses.append(tf.reduce_mean(-(log_score - group_norms)))
            loss = tf.add_n(losses)
        else:
            raise NotImplemented()
        tf.add_to_collection(tf.GraphKeys.LOSSES, loss)
        return BoundaryPrediction(tf.nn.softmax(masked_start_logits),
                                  tf.nn.softmax(masked_end_logits),
                                  masked_start_logits, masked_end_logits)


class IndependentBoundsSigmoidLoss(SpanFromBoundsPredictor):
    def __init__(self, aggregate="sum"):
        self.aggregate = aggregate

    def predict(self, answer, start_logits, end_logits, mask) -> Prediction:
        masked_start_logits = exp_mask(start_logits, mask)
        masked_end_logits = exp_mask(end_logits, mask)

        if len(answer) == 1:
            raise NotImplementedError()
        elif len(answer) == 2 and all(x.dtype == tf.bool for x in answer):
            # all correct start/end bounds are marked in a dense bool array
            # In this case there might be multiple answer spans, so we need an aggregation strategy
            losses = []
            for answer_mask, logits in zip(answer, [masked_start_logits, masked_end_logits]):
                losses.append(tf.nn.sigmoid_cross_entropy_with_logits(
                    labels=tf.cast(answer_mask, tf.float32),
                    logits=logits
                ))
            loss = tf.add_n(losses)
        else:
            raise NotImplemented()
        tf.add_to_collection(tf.GraphKeys.LOSSES, tf.reduce_mean(loss))
        return BoundaryPrediction(tf.nn.sigmoid(masked_start_logits),
                                  tf.nn.sigmoid(masked_end_logits),
                                  masked_start_logits, masked_end_logits)


class IndependentBoundsJointLoss(SpanFromBoundsPredictor):
    def predict(self, answer, start_logits, end_logits, mask) -> Prediction:
        if len(answer) != 1:
            raise NotImplementedError()
        masked_start_logits = exp_mask(start_logits, mask)
        masked_end_logits = exp_mask(end_logits, mask)
        answer_spans = answer[0]
        losses1 = tf.nn.sparse_softmax_cross_entropy_with_logits(
            logits=masked_start_logits, labels=answer_spans[:, 0])
        losses2 = tf.nn.sparse_softmax_cross_entropy_with_logits(
            logits=masked_end_logits, labels=answer_spans[:, 1])
        joint_loss = tf.reduce_logsumexp(tf.stack([losses1, losses2], axis=1), axis=1)
        loss = tf.reduce_mean(joint_loss)
        tf.add_to_collection(tf.GraphKeys.LOSSES, loss)
        return BoundaryPrediction(tf.nn.softmax(masked_start_logits),
                                  tf.nn.softmax(masked_end_logits),
                                  masked_start_logits, masked_end_logits)


class BoundedSpanPredictor(SpanFromBoundsPredictor):
    def __init__(self, bound: int, f1_weight=0, aggregate:str=None):
        self.bound = bound
        self.f1_weight = f1_weight
        self.aggregate = aggregate

    def predict(self, answer, start_logits, end_logits, mask) -> Prediction:
        bound = self.bound
        f1_weight = self.f1_weight
        aggregate = self.aggregate
        masked_logits1 = exp_mask(start_logits, mask)
        masked_logits2 = exp_mask(end_logits, mask)

        span_logits = []
        for i in range(self.bound):
            if i == 0:
                span_logits.append(masked_logits1 + masked_logits2)
            else:
                span_logits.append(masked_logits1[:, :-i] + masked_logits2[:, i:])
        span_logits = tf.concat(span_logits, axis=1)
        l = tf.shape(start_logits)[1]

        if len(answer) == 1:
            answer = answer[0]
            if answer.dtype == tf.int32:
                if f1_weight == 0:
                    answer_ix = to_packed_coordinates(answer, l, bound)
                    loss = tf.reduce_mean(
                        tf.nn.sparse_softmax_cross_entropy_with_logits(logits=span_logits, labels=answer_ix))
                else:
                    print("F1 mask!")
                    f1_mask = packed_span_f1_mask(answer, l, bound)
                    if f1_weight < 1:
                        f1_mask *= f1_weight
                        f1_mask += (1 - f1_weight) * tf.one_hot(to_packed_coordinates(answer, l, bound), l)
                    # TODO can we stay in log space?  (actually its tricky since f1_mask can have zeros...)
                    probs = tf.nn.softmax(span_logits)
                    loss = -tf.reduce_mean(tf.log(tf.reduce_sum(probs * f1_mask, axis=1)))
            else:
                log_norm = tf.reduce_logsumexp(span_logits, axis=1)
                if aggregate == "sum":
                    log_score = tf.reduce_logsumexp(
                        span_logits + VERY_NEGATIVE_NUMBER * (1 - tf.cast(answer, tf.float32)),
                        axis=1)
                elif aggregate == "max":
                    log_score = tf.reduce_max(span_logits + VERY_NEGATIVE_NUMBER * (1 - tf.cast(answer, tf.float32)),
                                              axis=1)
                else:
                    raise NotImplementedError()
                loss = tf.reduce_mean(-(log_score - log_norm))
        else:
            raise NotImplementedError()

        tf.add_to_collection(tf.GraphKeys.LOSSES, loss)
        return PackedSpanPrediction(span_logits, l, bound)


class SpanFromVectorBound(SequencePredictionLayer):
    def __init__(self,
                 mapper: SequenceBiMapper,
                 pre_process: Optional[SequenceMapper],
                 merge: MergeLayer,
                 post_process: Optional[Mapper],
                 bound: int,
                 f1_weight=0,
                 init: str="glorot_uniform",
                 aggregate="sum"):
        self.mapper = mapper
        self.pre_process = pre_process
        self.merge = merge
        self.post_process = post_process
        self.init = init
        self.f1_weight = f1_weight
        self.bound = bound
        self.aggregate = aggregate

    def apply(self, is_train, context_embed, answer, context_mask=None):
        init_fn = get_keras_initialization(self.init)
        bool_mask = tf.sequence_mask(context_mask, tf.shape(context_embed)[1])

        with tf.variable_scope("predict"):
            m1, m2 = self.mapper.apply(is_train, context_embed, context_mask)

        if self.pre_process is not None:
            with tf.variable_scope("pre-process1"):
                m1 = self.pre_process.apply(is_train, m1, context_mask)
            with tf.variable_scope("pre-process2"):
                m2 = self.pre_process.apply(is_train, m2, context_mask)

        span_vector_lst = []
        mask_lst = []
        with tf.variable_scope("merge"):
            span_vector_lst.append(self.merge.apply(is_train, m1, m2))
        mask_lst.append(bool_mask)
        for i in range(1, self.bound):
            with tf.variable_scope("merge", reuse=True):
                span_vector_lst.append(self.merge.apply(is_train, m1[:, :-i], m2[:, i:]))
            mask_lst.append(bool_mask[:, i:])

        mask = tf.concat(mask_lst, axis=1)
        span_vectors = tf.concat(span_vector_lst, axis=1)  # all logits -> flattened per-span predictions

        if self.post_process is not None:
            with tf.variable_scope("post-process"):
                span_vectors = self.post_process.apply(is_train, span_vectors)

        with tf.variable_scope("compute_logits"):
            logits = fully_connected(span_vectors, 1, activation_fn=None, weights_initializer=init_fn)

        logits = tf.squeeze(logits, squeeze_dims=[2])
        logits = logits + VERY_NEGATIVE_NUMBER * (1 - tf.cast(tf.concat(mask, axis=1), tf.float32))

        l = tf.shape(context_embed)[1]

        if len(answer) == 1:
            answer = answer[0]
            if answer.dtype == tf.int32:
                if self.f1_weight == 0:
                    answer_ix = to_packed_coordinates(answer, l, self.bound)
                    loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=answer_ix))
                else:
                    f1_mask = packed_span_f1_mask(answer, l, self.bound)
                    if self.f1_weight < 1:
                        f1_mask *= self.f1_weight
                        f1_mask += (1-self.f1_weight) * tf.one_hot(to_packed_coordinates(answer, l, self.bound), l)

                    # TODO can we stay in log space?  (actually its tricky since f1_mask can have zeros...)
                    probs = tf.nn.softmax(logits)
                    loss = -tf.reduce_mean(tf.log(tf.reduce_sum(probs * f1_mask, axis=1)))
            else:
                log_norm = tf.reduce_logsumexp(logits, axis=1)
                if self.aggregate == "sum":
                    log_score = tf.reduce_logsumexp(
                        logits + VERY_NEGATIVE_NUMBER * (1 - tf.cast(answer, tf.float32)),
                        axis=1)
                elif self.aggregate == "max":
                    log_score = tf.reduce_max(logits + VERY_NEGATIVE_NUMBER * (1 - tf.cast(answer, tf.float32)),
                                              axis=1)
                else:
                    raise NotImplementedError()
                loss = tf.reduce_mean(-(log_score - log_norm))
        else:
            raise NotImplementedError()

        tf.add_to_collection(tf.GraphKeys.LOSSES, loss)
        return PackedSpanPrediction(logits, l, self.bound)


class BoundsPredictor(SequencePredictionLayer):
    def __init__(self, predictor: SequenceBiMapper, init: str="glorot_uniform",
                 span_predictor: SpanFromBoundsPredictor = IndependentBounds()):
        self.predictor = predictor
        self.init = init
        self.span_predictor = span_predictor

    def apply(self, is_train, context_embed, answer, context_mask=None):
        init_fn = get_keras_initialization(self.init)
        with tf.variable_scope("bounds_encoding"):
            m1, m2 = self.predictor.apply(is_train, context_embed, context_mask)

        with tf.variable_scope("start_pred"):
            logits1 = fully_connected(m1, 1, activation_fn=None,
                                      weights_initializer=init_fn)
            logits1 = tf.squeeze(logits1, squeeze_dims=[2])

        with tf.variable_scope("end_pred"):
            logits2 = fully_connected(m2, 1, activation_fn=None, weights_initializer=init_fn)
            logits2 = tf.squeeze(logits2, squeeze_dims=[2])

        with tf.variable_scope("predict_span"):
            return self.span_predictor.predict(answer, logits1, logits2, context_mask)

    def __setstate__(self, state):
        if "aggregate" in state["state"]:
            state["state"]["bound_predictor"] = IndependentBounds(state["state"]["aggregate"])
        elif "bound_predictor" not in state:
            state["state"]["bound_predictor"] = IndependentBounds()
        super().__setstate__(state)


class WithFixedContextPredictionLayer(AttentionPredictionLayer):
    def __init__(self, context_mapper: SequenceMapper, context_encoder: SequenceEncoder,
                 merge: FixedMergeLayer, bounds_predictor: SequenceBiMapper,
                 init="glorot_uniform",
                 span_predictor: SpanFromBoundsPredictor = IndependentBounds()):
        self.context_mapper = context_mapper
        self.context_encoder = context_encoder
        self.bounds_predictor = bounds_predictor
        self.merge = merge
        self.init = init
        self.span_predictor = span_predictor

    def apply(self, is_train, x, memories, answer: List[Tensor], x_mask=None, memory_mask=None):
        with tf.variable_scope("map_context"):
            memories = self.context_mapper.apply(is_train, memories, memory_mask)
        with tf.variable_scope("encode_context"):
            encoded = self.context_encoder.apply(is_train, memories, memory_mask)
        with tf.variable_scope("merge"):
            x = self.merge.apply(is_train, x, encoded, x_mask)
        with tf.variable_scope("predict"):
            m1, m2 = self.bounds_predictor.apply(is_train, x, x_mask)

        init = get_keras_initialization(self.init)
        with tf.variable_scope("logits1"):
            l1 = fully_connected(m1, 1, activation_fn=None, weights_initializer=init)
            l1 = tf.squeeze(l1, squeeze_dims=[2])
        with tf.variable_scope("logits2"):
            l2 = fully_connected(m2, 1, activation_fn=None, weights_initializer=init)
            l2 = tf.squeeze(l2, squeeze_dims=[2])

        with tf.variable_scope("predict_span"):
            return self.span_predictor.predict(answer, l1, l2, x_mask)

    def __setstate__(self, state):
        if "aggregate" in state["state"]:
            state["state"]["span_predictor"] = IndependentBounds(state["state"]["aggregate"])
        elif "span_predictor" not in state["state"]:
            state["state"]["span_predictor"] = IndependentBounds()
        super().__setstate__(state)


class ConfidencePredictor(SequencePredictionLayer):
    """
    Optimize log probabilty of picking the correct span, or selecting a no-op, where
    the probability is P(op)P(start)P(end) if a span exists otherwise 1 - P(nop)
    This reduces op_logit + start_logit + end_logit (where an answer exists) -
        1 - op_logit (where answer exists)
    """
    def __init__(self,
                 predictor: SequenceBiMapper,
                 encoder: Union[SequenceEncoder, SequenceMultiEncoder],
                 confidence_predictor: Mapper,
                 init: str="glorot_uniform",
                 aggregate=None):
        self.predictor = predictor
        self.init = init
        self.aggregate = aggregate
        self.confidence_predictor = confidence_predictor
        self.encoder = encoder

    @property
    def version(self):
        return 1  # Fix maxing

    def apply(self, is_train, context_embed, answer, context_mask=None):
        init_fn = get_keras_initialization(self.init)
        m1, m2 = self.predictor.apply(is_train, context_embed, context_mask)

        if m1.shape.as_list()[-1] != 1:
            with tf.variable_scope("start_pred"):
                start_logits = fully_connected(m1, 1, activation_fn=None,
                                          weights_initializer=init_fn)
        else:
            start_logits = m1
        start_logits = tf.squeeze(start_logits, squeeze_dims=[2])

        if m1.shape.as_list()[-1] != 1:
            with tf.variable_scope("end_pred"):
                end_logits = fully_connected(m2, 1, activation_fn=None, weights_initializer=init_fn)
        else:
            end_logits = m2
        end_logits = tf.squeeze(end_logits, squeeze_dims=[2])

        masked_start_logits = exp_mask(start_logits, context_mask)
        masked_end_logits = exp_mask(end_logits, context_mask)

        start_atten = tf.einsum("ajk,aj->ak", m1, tf.nn.softmax(masked_start_logits))
        end_atten = tf.einsum("ajk,aj->ak", m2, tf.nn.softmax(masked_end_logits))
        with tf.variable_scope("encode_context"):
            enc = self.encoder.apply(is_train, context_embed, context_mask)
        if len(enc.shape) == 3:
            _, encodings, fe = enc.shape.as_list()
            enc = tf.reshape(enc, (-1, encodings*fe))

        with tf.variable_scope("confidence"):
            conf = [start_atten, end_atten, enc]
            none_logit = self.confidence_predictor.apply(is_train, tf.concat(conf, axis=1))
        with tf.variable_scope("confidence_logits"):
            none_logit = fully_connected(none_logit, 1, activation_fn=None,
                                   weights_initializer=init_fn)
            none_logit = tf.squeeze(none_logit, axis=1)

        batch_dim = tf.shape(start_logits)[0]

        # (batch, (l * l)) logits for each (start, end) pair

        all_logits = tf.reshape(tf.expand_dims(masked_start_logits, 1) +
                                tf.expand_dims(masked_end_logits, 2),
                                (batch_dim, -1))

        # (batch, (l * l) + 1) logits including the none option
        all_logits = tf.concat([all_logits, tf.expand_dims(none_logit, 1)], axis=1)
        log_norms = tf.reduce_logsumexp(all_logits, axis=1)

        # Now build a "correctness" mask in the same format
        correct_mask = tf.logical_and(tf.expand_dims(answer[0], 1), tf.expand_dims(answer[1], 2))
        correct_mask = tf.reshape(correct_mask, (batch_dim, -1))
        correct_mask = tf.concat([correct_mask, tf.logical_not(tf.reduce_any(answer[0], axis=1, keep_dims=True))], axis=1)

        log_correct = tf.reduce_logsumexp(all_logits + VERY_NEGATIVE_NUMBER * (1 - tf.cast(correct_mask, tf.float32)), axis=1)
        loss = tf.reduce_mean(-(log_correct - log_norms))
        probs = tf.nn.softmax(all_logits)
        tf.add_to_collection(tf.GraphKeys.LOSSES, loss)
        return ConfidencePrediction(probs[:, :-1], masked_start_logits, masked_end_logits,
                                    probs[:, -1], none_logit)


