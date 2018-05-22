# ------------------------------------------------------------------------
# Copyright (C) 2016-2017 Andrzej Pronobis - All Rights Reserved
#
# This file is part of LibSPN. Unauthorized use or copying of this file,
# via any medium is strictly prohibited. Proprietary and confidential.
# ------------------------------------------------------------------------

from collections import namedtuple
import tensorflow as tf
from libspn.inference.mpe_path import MPEPath
from libspn.inference.gradient import Gradient
from libspn.graph.algorithms import traverse_graph
from libspn.learning.type import LearningType
from libspn.learning.type import LearningInferenceType
from libspn import conf
from libspn.graph.distribution import GaussianLeaf


class GDLearning():
    """Assembles TF operations performing Gradient Descent learning of an SPN.

    Args:
        log (bool): If ``True``, calculate the value in the log space. Ignored
                    if ``mpe_path`` is given.
        value_inference_type (InferenceType): The inference type used during the
            upwards pass through the SPN. Ignored if ``mpe_path`` is given.
        learning_rate (float): Learning rate parameter used for updating SPN weights.
        learning_type (LearningType): Learning type used while learning.
        learning_inference_type (LearningInferenceType): Learning inference type
            used while learning.
    """
    ParamNode = namedtuple("ParamNode", ["node", "name_scope", "accum"])
    GaussianLeafNode = namedtuple("ParamNode", ["node", "name_scope", "mean_grad", "var_grad"])

    def __init__(self, root, mpe_path=None, gradient=None, learning_rate=0.1,
                 log=True, value_inference_type=None,
                 learning_type=LearningType.DISCRIMINATIVE,
                 learning_inference_type=LearningInferenceType.HARD,
                 add_random=None, use_unweighted=False):
        self._root = root
        if learning_rate <= 0.0:
            raise ValueError("learning_rate must be a positive number")
        else:
            self._learning_rate = learning_rate
        self._log = log
        self._learning_type = learning_type
        self._learning_inference_type = learning_inference_type
        if self._learning_inference_type == LearningInferenceType.HARD:
            self._gradient = None
            # Create internal MPE path generator
            if mpe_path is None:
                self._mpe_path = MPEPath(log=log,
                                         value_inference_type=value_inference_type,
                                         add_random=add_random,
                                         use_unweighted=use_unweighted)
            else:
                self._mpe_path = mpe_path
                self._log = mpe_path.log
        else:
            self._mpe_path = None
            # Create internal gradient generator
            if gradient is None:
                self._gradient = \
                    Gradient(log=log, value_inference_type=value_inference_type)
            else:
                self._gradient = gradient
                self._log = gradient.log
        # Create a name scope
        with tf.name_scope("GDLearning") as self._name_scope:
            pass
        # Create accumulators
        self._create_accumulators()

    @property
    def mpe_path(self):
        """MPEPath: Computed MPE path."""
        return self._mpe_path

    @property
    def gradient(self):
        """Gradient: Computed gradients."""
        return self._gradient

    @property
    def value(self):
        """Value or LogValue: Computed SPN values."""
        if self._learning_inference_type == LearningInferenceType.HARD:
            return self._mpe_path.value
        else:
            return self._gradient.value

    # TODO: For testing only
    def root_accum(self):
        for pn in self._param_nodes:
            if pn.node == self._root.weights.node:
                return pn.accum
        return None

    def reset_accumulators(self):
        with tf.name_scope(self._name_scope):
            return tf.group(*(
                [pn.accum.initializer for pn in self._param_nodes] +
                [gn.mean_grad.initializer for gn in self._gaussian_leaf_nodes] +
                [gn.var_grad.initializer for gn in self._gaussian_leaf_nodes]
            ), name="reset_accumulators")

    def accumulate_updates(self):
        if self._learning_inference_type == LearningInferenceType.HARD:
            # Generate path if not yet generated
            if not self._mpe_path.counts:
                self._mpe_path.get_mpe_path(self._root)

            if self._learning_type == LearningType.DISCRIMINATIVE and \
               not self._mpe_path.actual_counts:
                self._mpe_path.get_mpe_path_actual(self._root)
        else:
            # Generate gradients if not yet generated
            if not self._gradient.gradients:
                self._gradient.get_gradients(self._root)

            if self._learning_type == LearningType.DISCRIMINATIVE and \
               not self._gradient.actual_gradients:
                self._gradient.get_actual_gradients(self._root)

        # Generate all accumulate operations
        with tf.name_scope(self._name_scope):
            assign_ops = []
            for pn in self._param_nodes:
                with tf.name_scope(pn.name_scope):
                    if self._learning_inference_type == LearningInferenceType.HARD:
                        counts = self._mpe_path.counts[pn.node]
                        actual_counts = self._mpe_path.actual_counts[pn.node] if \
                            self._learning_type == LearningType.DISCRIMINATIVE else None
                        update_value = \
                            pn.node._compute_hard_gd_update(counts, actual_counts)
                        # Apply learning-rate
                        update_value *= self._learning_rate
                        op = tf.assign_add(pn.accum, update_value)
                        assign_ops.append(op)
                    else:
                        gradients = self._gradient.gradients[pn.node]
                        actual_gradients = self._gradient.actual_gradients[pn.node] \
                            if self._learning_type == LearningType.DISCRIMINATIVE \
                            else None
                        # TODO: Is there a better way to do this?
                        update_value = \
                            pn.node._compute_hard_gd_update(gradients, actual_gradients)
                        # Apply learning-rate
                        update_value *= self._learning_rate
                        op = tf.assign_add(pn.accum, update_value)
                        assign_ops.append(op)

            if self._learning_inference_type == LearningInferenceType.HARD:
                positive_grad_table = self._mpe_path.counts
                negative_grad_table = self._mpe_path.actual_counts
            else:
                positive_grad_table = self._gradient.gradients
                negative_grad_table = self._gradient.actual_gradients

            for gn in self._gaussian_leaf_nodes:
                with tf.name_scope(gn.name_scope):
                    incoming_grad = positive_grad_table[gn.node]
                    if self._learning_type == LearningType.DISCRIMINATIVE:
                        incoming_grad -= negative_grad_table[gn.node]
                    mean_grad, var_grad = gn.node._compute_gradient(incoming_grad)
                    assign_ops.append(tf.assign_add(gn.mean_grad, mean_grad * self._learning_rate))
                    assign_ops.append(tf.assign_add(gn.var_grad, var_grad * self._learning_rate))

            return tf.group(*assign_ops, name="accumulate_updates")

    def update_spn(self):
        # Generate all update operations
        with tf.name_scope(self._name_scope):
            assign_ops = []
            for pn in self._param_nodes:
                with tf.name_scope(pn.name_scope):
                    if self._learning_inference_type == LearningInferenceType.HARD:
                        # Add accumulators to respective weights
                        if pn.node.log:
                            assign_ops.append(pn.node.update_log(pn.accum))
                        else:
                            assign_ops.append(pn.node.update(pn.accum))
                    else:
                        # Add gradients to respective weights
                        if pn.node.log:
                            assign_ops.append(pn.node.update_log(pn.accum))
                        else:
                            assign_ops.append(pn.node.update(pn.accum))
            for gn in self._gaussian_leaf_nodes:
                with tf.name_scope(gn.name_scope):
                    assign_ops.extend(gn.node.assign_add(gn.mean_grad, gn.var_grad))
            return tf.group(*assign_ops, name="update_spn")

    def _create_accumulators(self):
        def fun(node):
            if node.is_param:
                with tf.name_scope(node.name) as scope:
                    accum = tf.Variable(tf.zeros_like(node.variable, dtype=conf.dtype),
                                        dtype=conf.dtype, collections=['gd_accumulators'])
                    param_node = GDLearning.ParamNode(node=node, accum=accum,
                                                      name_scope=scope)
                    self._param_nodes.append(param_node)

            if isinstance(node, GaussianLeaf) and node.learn_distribution_parameters:
                with tf.name_scope(node.name) as scope:
                    mean_grad_accum = tf.Variable(
                        tf.zeros_like(node.loc_variable, dtype=conf.dtype),
                        dtype=conf.dtype, collections=['gd_accumulators'])
                    variance_grad_accum = tf.Variable(
                        tf.zeros_like(node.scale_variable, dtype=conf.dtype),
                        dtype=conf.dtype, collections=['gd_accumulators'])
                    gauss_leaf_node = GDLearning.GaussianLeafNode(
                        node=node, mean_grad=mean_grad_accum, var_grad=variance_grad_accum,
                        name_scope=scope)
                    self._gaussian_leaf_nodes.append(gauss_leaf_node)

        self._param_nodes = []
        self._gaussian_leaf_nodes = []
        with tf.name_scope(self._name_scope):
            traverse_graph(self._root, fun=fun)

    def learn(self):
        """Assemble TF operations performing gradient descent learning of the SPN."""
        return None
