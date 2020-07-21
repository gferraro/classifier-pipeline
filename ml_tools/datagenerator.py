import random
import logging
import tensorflow.keras as keras
import tensorflow as tf
import cv2
import numpy as np
import matplotlib.pyplot as plt
from ml_tools.dataset import TrackChannels

FRAME_SIZE = 48


class DataGenerator(keras.utils.Sequence):
    "Generates data for Keras"

    def __init__(
        self,
        dataset,
        labels,
        num_classes,
        batch_size,
        model_preprocess=None,
        dim=(FRAME_SIZE, FRAME_SIZE, 3),
        n_channels=5,
        shuffle=True,
        sequence_size=27,
        lstm=False,
        use_thermal=False,
        use_filtered=False,
    ):
        self.labels = labels
        self.model_preprocess = model_preprocess
        self.use_thermal = use_thermal
        self.use_filtered = use_filtered
        self.lstm = lstm
        # default
        if not self.use_thermal and not self.use_filtered and not self.lstm:
            self.use_filtered = True
        self.dim = dim
        self.augment = dataset.enable_augmentation
        self.batch_size = batch_size
        self.sequence_size = sequence_size
        self.dataset = dataset
        self.size = len(dataset.frame_samples)
        if not self.lstm:
            self.size = self.size
        self.indexes = np.arange(self.size)
        self.shuffle = shuffle
        self.n_classes = num_classes
        self.n_channels = n_channels
        self.on_epoch_end()

    def get_data(self):
        X, y, _ = self._data(self.indexes, to_categorical=False)
        return X, y

    def __len__(self):
        "Denotes the number of batches per epoch"

        return int(np.floor(self.dataset.frames / self.batch_size))

    def __getitem__(self, index):
        "Generate one batch of data"
        # Generate indexes of the batch
        indexes = self.indexes[index * self.batch_size : (index + 1) * self.batch_size]

        # Generate data
        X, y, clips = self._data(indexes)
        # if self.dataset.name == "train":
        #     #
        #     fig = plt.figure(figsize=(48, 48))
        #     for i in range(len(X)):
        #         axes = fig.add_subplot(4, 10, i + 1)
        #         axes.set_title(
        #             "{} - {} track {} frame {}".format(
        #                 self.labels[np.argmax(np.array(y[i]))],
        #                 clips[i].clip_id,
        #                 clips[i].track_id,
        #                 clips[i].frame_num,
        #             )
        #         )
        #         plt.imshow(tf.keras.preprocessing.image.array_to_img(X[i]))
        #     plt.savefig("testimage.png")
        #     plt.close(fig)
        # raise "save err"

        return X, y

    def on_epoch_end(self):
        "Updates indexes after each epoch"
        if self.shuffle:
            np.random.shuffle(self.indexes)

    def _data(self, indexes, to_categorical=True):
        "Generates data containing batch_size samples"  # X : (n_samples, *dim, n_channels)
        # Initialization
        if self.lstm:
            X = np.empty((len(indexes), self.sequence_size, *self.dim))
        else:
            X = np.empty((len(indexes), *self.dim))

        y = np.empty((len(indexes)), dtype=int)
        clips = []
        # Generate data
        for i, index in enumerate(indexes):
            segment_i = index
            frame = self.dataset.frame_samples[segment_i]
            data, label = self.dataset.fetch_frame(frame)
            if label not in self.labels:
                continue
            if self.lstm:
                raise "LSTM not implemented"
            else:
                data = preprocess_frame(
                    data,
                    self.dim,
                    self.use_thermal,
                    self.augment,
                    self.model_preprocess,
                )
                if data is None:
                    logging.error(
                        "error pre processing frame (i.e.max and min are the same) clip %s track %s frame %s",
                        frame.clip_id,
                        frame.track_id,
                        frame.frame_num,
                    )
                    continue

            X[i,] = data
            y[i] = self.labels.index(label)
            clips.append(frame)

        if to_categorical:
            return X, keras.utils.to_categorical(y, num_classes=self.n_classes), clips
        return X, y, clips


def resize(image, dim):
    image = convert(image)
    image = tf.image.resize(image, dim[0], dim[1])
    return image.numpy()


def reisze_cv(image, dim, interpolation=cv2.INTER_LINEAR, extra_h=0, extra_v=0):
    return cv2.resize(
        image, dsize=(dim[0] + extra_h, dim[1] + extra_v), interpolation=interpolation,
    )


def convert(image):
    image = tf.image.convert_image_dtype(image, tf.float32)
    return image


def augement_frame(frame, dim):
    frame = reisze_cv(
        frame,
        dim,
        extra_h=random.randint(0, int(FRAME_SIZE * 0.1)),
        extra_v=random.randint(0, int(FRAME_SIZE * 0.1)),
    )

    image = convert(frame)
    # image = tf.image.resize(
    #     image, [FRAME_SIZE + random.randint(0, 4), FRAME_SIZE + random.randint(0, 4)],
    # )  # Add 6 pixels of padding
    image = tf.image.random_crop(
        image, size=[dim[0], dim[1], 3]
    )  # Random crop back to 28x28
    if random.random() > 0.50:
        rotated = tf.image.rot90(image)
    if random.random() > 0.50:
        flipped = tf.image.flip_left_right(image)

        # maybes thisd should only be sometimes, as otherwise our validation set
    # if random.random() > 0.20:
    image = tf.image.random_contrast(image, 0.8, 1.2)
    # image = tf.image.random_brightness(image, max_delta=0.05)  # Random brightness
    image = tf.minimum(image, 1.0)
    image = tf.maximum(image, 0.0)
    return image.numpy()


def preprocess_frame(
    data, output_dim, use_thermal=True, augment=False, preprocess_fn=None
):
    if use_thermal:
        channel = TrackChannels.thermal
        print("thermal")
    else:
        channel = TrackChannels.filtered
    data = data[channel]
    print("pre values", data)
    # normalizes data, constrast stretch good or bad?
    if augment:
        percent = random.randint(0, 2)
        print("aug")
    else:
        percent = 0
    max = int(np.percentile(data, 100 - percent))
    min = int(np.percentile(data, percent))
    print(min, max)
    if max == min:
        #     logging.error(
        #         "frame max and min are the same clip %s track %s frame %s",
        #         frame.clip_id,
        #         frame.track_id,
        #         frame.frame_num,
        #     )
        return None

    data -= min
    data = data / (max - min)
    np.clip(data, a_min=0, a_max=None, out=data)

    data = data[np.newaxis, :]
    data = np.transpose(data, (1, 2, 0))
    data = np.repeat(data, output_dim[2], axis=2)

    if augment:
        data = augement_frame(data, output_dim)
        data = np.clip(data, a_min=0, a_max=None, out=data)
    else:
        data = reisze_cv(data, output_dim)

    # pre proce expects values in range 0-255
    if preprocess_fn:
        print("pre pro")
        data = data * 255
        data = preprocess_fn(data)
    return data
