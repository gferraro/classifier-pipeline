"""
Script to train model for classifying animals from thermal footage.
"""

import os.path
import numpy as np
import random
import pickle
import tensorflow as tf
import math
import matplotlib.pyplot as plt
import itertools
from importlib import reload
from ml_tools import tools
import json
from ml_tools.model import Model

import logging

"""
Things to try
Apparently recomendedation is 1e-8?? might try that
Gradient clipping on LSTM is a good idea, set to 10
Deeper
Batch norm after every weight layer?
Try 10 frames?
Do some performance analysis... could optimise this
"""

#------------------------------------------------------------
# Helper functions
#------------------------------------------------------------

def prod(data):
    x = 1
    for value in data:
        x *= int(value)
    return x

#------------------------------------------------------------

class Estimator():

    # todo: these should be in some settings file
    MODEL_NAME = "Model-4g"
    MODEL_DESCRIPTION = "CNN + LSTM"

    BATCH_SIZE = 32
    BATCH_NORM = True
    LEARNING_RATE = 2e-4 #2e-4
    LEARNING_RATE_DECAY = 0.9 #0.9
    L2_REG = 0.01 # 0.005
    LABEL_SMOOTHING = 0.1
    LSTM_UNITS = 256
    USE_PEEPHOLES = False # these don't really help.


    def __init__(self):

        self.train = None
        self.validation = None
        self.test = None

        # tensorflow placeholders
        self.keep_prob = None

    @property
    def datasets(self):
        return (self.train, self.validation, self.test)

    @property
    def num_classes(self):
        return len(self.train.classes)

    @property
    def classes(self):
        return self.train.classes

    def import_dataset(self, base_path, force_normalisation_constants = None):
        """
        Import dataset from basepath.
        :param base_path:
        :param force_normalisation_constants: If defined uses these normalisation constants rather than those
            saved with the dataset.
        :return:
        """


        self.train, self.validation, self.test = pickle.load(open(os.path.join(base_path, "datasets.dat"),'rb'))

        # augmentation really helps with reducing overfitting, but test set should be fixed so we don't apply it there.
        self.train.enable_augmentation = True

        logging.info("Training segments: {0:.1f}k".format(self.train.rows/1000))
        logging.info("Validation segments: {0:.1f}k".format(self.validation.rows/1000))
        logging.info("Test segments: {0:.1f}k".format(self.test.rows/1000))

        if force_normalisation_constants:
            print("Using custom normalisation constants.")
            for dataset in self.datasets:
                dataset.normalisation_constants = force_normalisation_constants

        # helpful to have the test set in memory.
        self.test.X, self.test.y = self.test.fetch_all()

    def _conv_layer(self, input_layer, filters, kernal_size, conv_stride=2, pool_stride=1):
        layer = tf.layers.conv2d(inputs=input_layer, filters=filters, kernel_size=kernal_size,
                                 strides=(conv_stride, conv_stride),
                                 padding="same", activation=None)
        tf.summary.histogram('preactivations', layer)
        if self.BATCH_NORM: layer = tf.contrib.layers.batch_norm(
            layer, center=True, scale=True,
            fused=True,
            is_training=(self.keep_prob == 1.0)
        )
        layer = tf.nn.relu(layer)
        if pool_stride != 1:
            layer = tf.layers.max_pooling2d(inputs=layer, pool_size=[pool_stride, pool_stride], strides=pool_stride)
        return layer

    def build_model(self):
        ####################################
        # CNN + LSTM
        # based on https://arxiv.org/pdf/1507.06527.pdf
        ####################################

        tf.reset_default_graph()

        # Define our model

        self.X = tf.placeholder(tf.float32, [None, 27, 64, 64, 5], name='X')

        self.y = tf.placeholder(tf.int64, [None], name='y')

        # Split up input

        # default keep_probability to 1.0 if not specified
        self.keep_prob = tf.placeholder_with_default(tf.constant(1.0, tf.float32), [], name='keep_prob')

        # channel dropout. Drop channels to force model to learn both motion and appearance
        #channel_dropout = tf.nn.dropout(tf.constant(1.0, tf.float32, [5]), keep_prob=self.keep_prob)
        #self.X = self.X * channel_dropout[tf.newaxis, tf.newaxis, tf.newaxis, tf.newaxis, :]

        # frame dropout.  Drop random frames to force LSTM to remember
        #frame_dropout = tf.nn.dropout(tf.constant(1.0, tf.float32, [27]), keep_prob=self.keep_prob)
        #self.X = self.X * frame_dropout[tf.newaxis, :, tf.newaxis, tf.newaxis, tf.newaxis]

        # first put all frames in batch into one line sequence
        X_reshaped = tf.reshape(self.X[:, :, :, :, 0:3+1], [-1, 64, 64, 4])

        # next run the convolutions
        c1 = self._conv_layer(X_reshaped[:, :, :, 1:2], 32, [8, 8], conv_stride=4)
        c2 = self._conv_layer(c1, 48, [4, 4], conv_stride=2)
        c3 = self._conv_layer(c2, 64, [3, 3], conv_stride=1)

        filtered_conv = c3

        c1 = self._conv_layer(X_reshaped[:, :, :, 2:4], 32, [8, 8], conv_stride=4)
        c2 = self._conv_layer(c1, 48, [4, 4], conv_stride=2)
        c3 = self._conv_layer(c2, 64, [3, 3], conv_stride=1)

        motion_conv = c3

        print("convolution output shape: ", filtered_conv.shape, motion_conv.shape)

        # reshape back into segments

        flat1 = tf.reshape(filtered_conv, [-1, 27, prod(filtered_conv.shape[1:])])
        flat2 = tf.reshape(motion_conv, [-1, 27, prod(motion_conv.shape[1:])])

        flat = tf.concat((flat1, flat2), axis=2)

        print('Flat', flat.shape, 'from', flat1.shape, ',', flat2.shape)

        # the LSTM expects an array of 27 examples
        sequences = tf.unstack(flat, 27, 1)

        print('Sequences', len(sequences), sequences[0].shape)

        # run the LSTM
        lstm_cell_fw = tf.contrib.rnn.LSTMCell(
            num_units=self.LSTM_UNITS, use_peepholes=self.USE_PEEPHOLES, cell_clip=10.0)
        lstm_cell_bk = tf.contrib.rnn.LSTMCell(
            num_units=self.LSTM_UNITS, use_peepholes=self.USE_PEEPHOLES, cell_clip=10.0)

        # lstm_outputs, lstm_states = tf.contrib.rnn.static_rnn(lstm_cell, sequences, dtype = 'float32')
        lstm_outputs, _, _ = tf.contrib.rnn.static_bidirectional_rnn(
            lstm_cell_fw, lstm_cell_bk, sequences,
            dtype=tf.float32)

        print("LSTM outputs:", len(lstm_outputs))

        # probably just need the final output, but concatinating the hidden state might help?
        lstm_output = lstm_outputs[-1]

        # print("Final output shape:",lstm_output.shape)
        print("lstm output shape:", lstm_output.shape)

        # skip dense layer... might be needed for more complex things, but need more data to train.
        h1 = tf.nn.dropout(lstm_output, keep_prob=self.keep_prob)

        # dense layer2
        logits = tf.layers.dense(inputs=h1, units=self.num_classes, activation=None, name='logits')

        # prediction with softmax
        class_out = tf.argmax(logits, axis=1, name='class_out')
        pred = tf.nn.softmax(logits, name='prediction')

        correct_prediction = tf.equal(class_out, self.y)
        accuracy = tf.reduce_mean(tf.cast(correct_prediction, dtype=tf.float32), name='accuracy')

        with tf.variable_scope('logits', reuse=True):
            h2_weights = tf.get_variable('kernel')

        reg_loss = (tf.nn.l2_loss(h2_weights) * self.L2_REG)
        loss = tf.add(
            tf.losses.softmax_cross_entropy(onehot_labels=tf.one_hot(self.y, self.num_classes), logits=logits,
                                            label_smoothing=self.LABEL_SMOOTHING), reg_loss,
            name='loss')

        # setup our training loss
        epoch_steps = self.train.rows
        global_step = tf.Variable(0, trainable=False)
        learning_rate = tf.train.exponential_decay(self.LEARNING_RATE, global_step, epoch_steps, self.LEARNING_RATE_DECAY,
                                                   staircase=True)

        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)

        # record some stats
        tf.summary.scalar('accuracy', accuracy)
        tf.summary.scalar('loss', loss)
        tf.summary.scalar('reg_loss', reg_loss)

        # make sure to update batch norms.
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            train_op = optimizer.minimize(loss=loss, name='train_op')

        # define our model
        self.model = Model(self.datasets, self.X, self.y, self.keep_prob, pred, accuracy, loss, train_op, self.classes)
        self.model.batch_size = self.BATCH_SIZE

    def start_async_load(self):
        self.train.start_async_load(512)
        self.validation.start_async_load(512)

    def stop_async(self):
        self.train.stop_async_load()
        self.validation.stop_async_load()

    def train_model(self, max_epochs=10, stop_after_no_improvement=20, stop_after_decline=3, log_dir = None):
        print("{0:.1f}K training examples".format(self.train.rows / 1000))
        self.model.train_model(max_epochs, keep_prob=0.4, stop_after_no_improvement=stop_after_no_improvement,
                               stop_after_decline=stop_after_decline, log_dir=log_dir)

    def save_model(self):
        """ Saves a copy of the current model. """
        score_part = "{:.3f}".format(self.model.eval_score)
        while len(score_part) < 3:
            score_part = score_part + "0"


        saver = tf.train.Saver()
        save_filename = os.path.join("./models/", self.MODEL_NAME + '-' + score_part)
        print("Saving", save_filename)
        saver.save(self.model.sess, save_filename)

        # save some additional data
        model_stats = {}
        model_stats['name'] = self.MODEL_NAME
        model_stats['description'] = self.MODEL_DESCRIPTION
        model_stats['notes'] = ""
        model_stats['classes'] = self.classes
        model_stats['score'] = self.model.eval_score
        model_stats['normalisation'] = self.train.normalisation_constants

        json.dump(model_stats, open(save_filename + ".txt", 'w'), indent=4)

def main():
    logging.basicConfig(level=0)
    tf.logging.set_verbosity(3)
    estimator = Estimator()

    # force normalisation levels
    normalisation_constants = [
            (-3200, 250, 1),
            (-2, 10, 1),
            (0, 1, 0.5),  # take squareroot of motion flow vectors.
            (0, 1, 0.5),
            (0, 1, 1)
        ]

    estimator.import_dataset("c://cac//bellbird/dataset//", normalisation_constants)

    estimator.build_model()
    estimator.start_async_load()

    #log_dir='./logs'
    estimator.train_model(
        max_epochs=25, stop_after_no_improvement=999, stop_after_decline=999)
    estimator.save_model()
    estimator.stop_async()



if __name__ == "__main__":
    # execute only if run as a script
    main()