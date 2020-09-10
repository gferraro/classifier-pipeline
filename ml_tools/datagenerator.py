from PIL import Image, ImageDraw, ImageFont, ImageColor
from pathlib import Path

import math
import random
import logging
import tensorflow.keras as keras
import tensorflow as tf
import cv2
import numpy as np
import matplotlib.pyplot as plt
from ml_tools.dataset import TrackChannels
import multiprocessing
import time
from ml_tools.dataset import Preprocessor
from ml_tools import tools
from ml_tools.imageprocessing import movement_images

FRAME_SIZE = 48
FRAMES_PER_SECOND = 9


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
        lstm=False,
        use_thermal=False,
        use_filtered=False,
        buffer_size=128,
        epochs=10,
        load_threads=1,
        preload=True,
        use_movement=False,
        balance_labels=True,
        keep_epoch=False,
        randomize_epoch=True,
        cap_samples=True,
        cap_at=None,
        type=0,
    ):
        self.type = type
        self.cap_at = cap_at
        self.cap_samples = cap_samples
        self.randomize_epoch = randomize_epoch
        self.use_previous_epoch = None
        self.keep_epoch = keep_epoch
        self.balance_labels = balance_labels

        self.labels = labels
        self.model_preprocess = model_preprocess
        self.use_thermal = use_thermal
        self.use_filtered = use_filtered
        self.lstm = lstm

        if not self.use_thermal and not self.use_filtered and not self.lstm:
            self.use_thermal = True
        self.movement = use_movement
        self.square_width = None
        if use_movement:
            self.square_width = int(math.sqrt(round(dataset.segment_length * 9)))
            dim = (dim[0] * self.square_width, dim[1] * self.square_width, dim[2])
        self.dim = dim
        self.augment = dataset.enable_augmentation
        self.batch_size = batch_size
        self.dataset = dataset
        self.samples = None
        self.shuffle = shuffle
        self.n_classes = len(self.labels)
        self.n_channels = n_channels
        self.cur_epoch = 0
        self.epochs = epochs
        self.epoch_data = []
        self.preload = preload
        if self.preload:
            self.load_queue = multiprocessing.Queue()

        self.load_next_epoch()
        self.on_epoch_end()
        self.cur_epoch = 0

        if self.preload:
            self.preloader_queue = multiprocessing.Queue(buffer_size)
            self.preloader_stop_flag = False

            self.preloader_threads = [
                multiprocessing.Process(
                    target=preloader, args=(self.preloader_queue, self.load_queue, self)
                )
                for _ in range(load_threads)
            ]
            for thread in self.preloader_threads:
                thread.start()
        print(
            "datagen for ",
            self.dataset.name,
            " shuffle?",
            self.shuffle,
            " cap",
            self.cap_samples,
        )

    def stop_load(self):
        if not self.preload:
            return
        self.preloader_stop_flag = True
        for thread in self.preloader_threads:
            if hasattr(thread, "terminate"):
                # note this will corrupt the queue, so reset it
                thread.terminate()
                self.preloader_queue = None
            else:
                thread.exit()

    def get_epoch_predictions(self, epoch=-1):
        if self.keep_epoch:
            return self.epoch_data[epoch][1]
        return None

    # get all data
    def get_data(self, epoch=0, catog=False):
        X, y = self._data(self.samples, to_categorical=catog)
        return X, y

    def __len__(self):
        "Denotes the number of batches per epoch"

        return int(math.ceil(len(self.samples) / self.batch_size))

    def loadbatch(self, index):
        start = time.time()
        samples = self.samples[index * self.batch_size : (index + 1) * self.batch_size]
        X, y = self._data(samples)
        logging.debug("Time to get data %s", time.time() - start)

        return X, y

    def __getitem__(self, index):
        "Generate one batch of data"
        # Generate indexes of the batch
        logging.debug("%s requsting index %s", self.dataset.name, index)
        if self.keep_epoch and self.use_previous_epoch is not None:
            return (
                self.epoch_data[self.use_previous_epoch][0],
                self.epoch_data[self.use_previous_epoch][1],
            )
        if self.preload:
            X, y = self.preloader_queue.get()
        else:
            X, y = self.loadbatch(index)
        if self.keep_epoch:
            self.epoch_data[self.cur_epoch][0][index] = None
            self.epoch_data[self.cur_epoch][1][index] = y
            # (X, y))
        if (index + 1) == len(self):
            self.load_next_epoch()
        return X, y

    def load_next_epoch(self):
        # if self.type == 9:
        #     self.dataset.random_segments()
        self.samples = self.dataset.epoch_samples(
            cap_samples=self.cap_samples,
            replace=False,
            random=self.randomize_epoch,
            cap_at=self.cap_at,
        )
        labels = set([sample.track.label for sample in self.samples])
        for label in labels:
            logging.info(
                "%s epoch %s lbl %s has %s",
                self.dataset.name,
                self.cur_epoch,
                label,
                len(
                    [
                        sample.track.label
                        for sample in self.samples
                        if sample.label == label
                    ]
                )
                / len(self.samples)
                * 200,
            )
        if self.shuffle:
            np.random.shuffle(self.samples)
        if self.preload:
            # for some reason it always requests 0 twice
            self.load_queue.put(0)
            for i in range(len(self)):
                self.load_queue.put(i)

    def on_epoch_end(self):
        "Updates indexes after each epoch"
        batches = len(self)
        self.epoch_data.append(([None] * batches, [None] * batches))
        logging.debug("epoch ended for %s", self.dataset.name)
        self.cur_epoch += 1

    def _data(self, samples, to_categorical=True):
        "Generates data containing batch_size samples"
        # Initialization
        if self.lstm:
            X = np.empty((len(samples), samples[0].frames, *self.dim))
        else:
            X = np.empty((len(samples), *self.dim,))

        y = np.empty((len(samples)), dtype=int)
        # Generate data
        data_i = 0
        if self.use_thermal:
            channel = TrackChannels.thermal
        else:
            channel = TrackChannels.filtered

        for sample in samples:
            if self.movement:
                try:
                    if self.type >= 3:
                        data = self.dataset.fetch_track(sample.track, preprocess=False)

                    else:
                        data = self.dataset.fetch_segment(sample, preprocess=False)
                except Exception as inst:
                    logging.error("Error fetching sample %s %s", sample, inst)
                    continue
                label = self.dataset.mapped_label(sample.label)
                if self.type == 3:
                    segment_data = data[
                        sample.start_frame : sample.start_frame + sample.frames
                    ]
                    ref = sample.track.frame_temp_median[
                        sample.start_frame : sample.start_frame + len(segment_data)
                    ]
                elif self.type >= 4:

                    frames = np.random.choice(
                        sample.track.important_frames,
                        min(sample.frames, len(sample.track.important_frames)),
                        replace=False,
                    )
                    # print("using frames", frames)
                    segment_data = []
                    ref = []

                    frames = [frame.frame_num for frame in frames]
                    # sort??? or rather than random just apply a 1 second step

                    frames.sort()
                    for frame_num in frames:
                        segment_data.append(data[frame_num])
                        ref.append(sample.track.frame_temp_median[frame_num])
                else:
                    segment_data = data
                    ref = sample.track.frame_temp_median[
                        sample.start_frame : sample.start_frame + len(data)
                    ]
                segment, flipped = Preprocessor.apply(
                    segment_data, ref, augment=self.augment, keep_aspect=self.type == 6
                )
                if self.type < 3:
                    regions = sample.track.track_bounds[
                        sample.start_frame : sample.start_frame + sample.frames
                    ]
                else:
                    regions = sample.track.track_bounds

                data = preprocess_movement(
                    data,
                    segment,
                    self.square_width,
                    regions,
                    channel,
                    self.model_preprocess,
                    sample,
                    self.dataset.name,
                    self.type,
                    flip=flipped,
                )
            else:
                try:
                    data, label = self.dataset.fetch_sample(
                        sample, augment=self.augment, channels=channel
                    )

                    if label not in self.labels:
                        continue
                except Exception as inst:
                    logging.error("Error fetching samples %s %s", sample, inst)
                    continue
                if self.lstm:
                    data = preprocess_lstm(
                        data, self.dim, channel, self.augment, self.model_preprocess,
                    )
                else:
                    data = preprocess_frame(
                        data, self.dim, None, self.augment, self.model_preprocess,
                    )
            if data is None:
                logging.warn(
                    "error pre processing frame (i.e.max and min are the same)sample %s",
                    sample,
                )
                continue

            X[data_i] = data
            y[data_i] = self.labels.index(label)
            data_i += 1
        # remove data that was null
        X = X[:data_i]
        y = y[:data_i]
        if to_categorical:
            y = keras.utils.to_categorical(y, num_classes=self.n_classes)
        return np.array(X), y


def resize(image, dim):
    image = convert(image)
    image = tf.image.resize(image, dim[0], dim[1])
    return image.numpy()


def resize_cv(image, dim, interpolation=cv2.INTER_LINEAR, extra_h=0, extra_v=0):
    return cv2.resize(
        image, dsize=(dim[0] + extra_h, dim[1] + extra_v), interpolation=interpolation,
    )


def convert(image):
    image = tf.image.convert_image_dtype(image, tf.float32)
    return image


def augement_frame(frame, dim):
    frame = resize_cv(
        frame,
        dim,
        extra_h=random.randint(0, int(FRAME_SIZE * 0.05)),
        extra_v=random.randint(0, int(FRAME_SIZE * 0.05)),
    )

    image = convert(frame)
    # image = tf.image.resize(
    #     image, [FRAME_SIZE + random.randint(0, 4), FRAME_SIZE + random.randint(0, 4)],
    # )  # Add 6 pixels of padding
    image = tf.image.random_crop(
        image, size=[dim[0], dim[1], 3]
    )  # Random crop back to 48x 48
    # if random.random() > 0.50:
    #     image = tf.image.rot90(image)
    if random.random() > 0.50:
        image = tf.image.flip_left_right(image)

        # maybes thisd should only be sometimes, as otherwise our validation set
    # if random.random() > 0.20:
    image = tf.image.random_contrast(image, 0.8, 1.2)
    # image = tf.image.random_brightness(image, max_delta=0.05)  # Random brightness
    image = tf.minimum(image, 1.0)
    image = tf.maximum(image, 0.0)
    return image.numpy()


def square_clip(data, square_width, type=None):
    # lay each frame out side by side in rows
    frame_size = Preprocessor.FRAME_SIZE
    background = np.zeros((square_width * frame_size, square_width * frame_size))

    i = 0
    success = False
    for x in range(square_width):
        for y in range(square_width):
            if i >= len(data):
                # if type >= 4:
                #     frame_i = random.randint(0, len(data) - 1)
                #     frame = data[frame_i]
                # else:
                frame = data[-1]
            else:
                frame = data[i]
            frame, norm_success = normalize(frame)

            if not norm_success:
                continue
            success = True
            background[
                x * frame_size : (x + 1) * frame_size,
                y * frame_size : (y + 1) * frame_size,
            ] = np.float32(frame)
            i += 1

    return background, success


def square_clip_flow(data_flow_h, data_flow_v, square_width, type=None):
    # lay each frame out side by side in rows
    frame_size = Preprocessor.FRAME_SIZE
    background = np.zeros((square_width * frame_size, square_width * frame_size))

    i = 0
    success = False
    for x in range(square_width):
        for y in range(square_width):
            if i >= len(data_flow_h):
                if type >= 4:
                    frame_i = random.randint(0, len(data_flow_h) - 1)
                    flow_h = data_flow_h[frame_i]
                    flow_v = data_flow_v[frame_i]

                else:
                    flow_v = data_flow_v[-1]
                    flow_h = data_flow_h[-1]

            else:
                flow_v = data_flow_v[i]
                flow_h = data_flow_h[i]

            flow_magnitude = (
                np.linalg.norm(np.float32([flow_h, flow_v]), ord=2, axis=0) / 4.0
            )
            frame, norm_success = normalize(flow_magnitude)

            if not norm_success:
                continue
            success = True
            background[
                x * frame_size : (x + 1) * frame_size,
                y * frame_size : (y + 1) * frame_size,
            ] = np.float32(frame)
            i += 1

    return background, success


def preprocess_movement(
    data,
    segment,
    square_width,
    regions,
    channel,
    preprocess_fn=None,
    sample=None,
    dataset=None,
    type=0,
    flip=False,
):
    # doesn't seem to improve anything, infact makes worse
    # flip = False
    if type == 7:
        flow_h = segment[:, TrackChannels.flow_h]
        flow_v = segment[:, TrackChannels.flow_v]
        square_flow, success = square_clip_flow(flow_h, flow_v, square_width, type)
    if type == 8:
        square_therm, success = square_clip(
            segment[:, TrackChannels.thermal], square_width, type
        )

    segment = segment[:, channel]
    # as long as one frame is fine
    square, success = square_clip(segment, square_width, type)
    if not success:
        return None
    dots, overlay = movement_images(
        data, regions, square.shape, channel=channel, require_movement=type >= 5,
    )
    dots = dots / 255
    overlay, success = normalize(overlay, min=0)
    if not success:
        return None
    if flip and type == 10:
        overlay = np.flip(overlay, axis=1)
        dots = np.flip(dots, axis=1)

    data = np.empty((square.shape[0], square.shape[1], 3))
    if type == 0:
        data[:, :, 0] = square
        data[:, :, 1] = square  # dots
        data[:, :, 2] = square  # overlay
    elif type == 1:
        data[:, :, 0] = square
        data[:, :, 1] = square  # dots
        data[:, :, 2] = overlay  # overlay
    elif type == 7:
        data[:, :, 0] = square
        data[:, :, 1] = square_flow  # dots
        data[:, :, 2] = overlay  # overlay
    elif type == 8:
        data[:, :, 0] = square
        data[:, :, 1] = square_therm  # dots
        data[:, :, 2] = overlay  # overlay
    else:
        data[:, :, 0] = square
        data[:, :, 1] = dots  # dots
        data[:, :, 2] = overlay  # overlay

    # #
    # savemovement(
    #     data,
    #     "samples/{}/{}/{}-{}-{}".format(
    #         dataset, sample.label, sample.track.clip_id, sample.track.track_id, flip
    #     ),
    # )

    if preprocess_fn:
        for i, frame in enumerate(data):
            frame = frame * 255
            data[i] = preprocess_fn(frame)
    return data


def preprocess_lstm(
    data, output_dim, channel, augment=False, preprocess_fn=None,
):

    data = data[:, channel]
    # normalizes data, constrast stretch good or bad?
    # if augment:
    #     percent = random.randint(0, 2)
    # else:
    data, success = normalize(data)
    np.clip(data, a_min=0, a_max=None, out=data)
    data = data[..., np.newaxis]

    # data = np.transpose(data, (2, 3, 0))
    data = np.repeat(data, output_dim[2], axis=3)
    # pre proce expects values in range 0-255
    if preprocess_fn:
        for i, frame in enumerate(data):

            frame = frame * 255
            data[i] = preprocess_fn(frame)
    return data


def preprocess_frame(
    data, output_dim, channel, augment=False, preprocess_fn=None,
):
    if channel is not None:
        data = data[channel]
    data, success = normalize(data)
    np.clip(data, a_min=0, a_max=None, out=data)
    data = data[..., np.newaxis]
    data = np.repeat(data, 3, axis=2)

    if augment:
        data = augement_frame(data, output_dim)
        data = np.clip(data, a_min=0, a_max=None, out=data)
    else:
        data = resize_cv(data, output_dim)
        data = convert(data)

    # pre proce expects values in range 0-255
    if preprocess_fn:
        data = data * 255
        data = preprocess_fn(data)
    return data


# continue to read examples until queue is full
def preloader(q, load_queue, dataset):
    """ add a segment into buffer """
    logging.info(
        " -started async fetcher for %s augment=%s",
        dataset.dataset.name,
        dataset.augment,
    )
    while not dataset.preloader_stop_flag:
        if not q.full():
            batch_i = load_queue.get()
            q.put(dataset.loadbatch(batch_i))
            if batch_i + 1 == len(dataset):
                dataset.cur_epoch += 1
            logging.debug(
                "Preloader %s loaded batch epoch %s batch %s",
                dataset.dataset.name,
                dataset.cur_epoch,
                batch_i,
            )
        else:
            time.sleep(0.1)


def saveimages(X, filename="testimage"):
    fig = plt.figure(figsize=(100, 100))
    rows = int(np.ceil(len(X) / 20))
    for x_i, x_img in enumerate(X):
        axes = fig.add_subplot(rows, 20, x_i + 1)
        plt.axis("off")
        x_img = np.asarray(x_img)
        if len(x_img.shape) == 2:
            x_img = x_img[..., np.newaxis]
            x_img = np.repeat(x_img, 3, axis=2)

        img = plt.imshow(tf.keras.preprocessing.image.array_to_img(x_img))
        img.set_cmap("hot")

    plt.savefig("{}.png".format(filename), bbox_inches="tight")
    plt.close(fig)


def normalize(data, min=None, max=None, new_max=1):
    if max is None:
        max = np.amax(data)
    if min is None:
        min = np.amin(data)
    if max == min:
        if max == 0:
            return np.zeros((data.shape)), False
        return data / max, False
    data -= min
    data = data / (max - min) * new_max
    return data, True


def savemovement(data, filename):
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    r = Image.fromarray(np.uint8(data[:, :, 0] * 255))
    g = Image.fromarray(np.uint8(data[:, :, 1] * 255))
    b = Image.fromarray(np.uint8(data[:, :, 2] * 255))
    concat = np.concatenate((r, g, b), axis=1)  # horizontally
    img = Image.fromarray(np.uint8(concat))
    d = ImageDraw.Draw(img)
    img.save(filename + "rgb.png")


def savebatch(X, y):
    fig = plt.figure(figsize=(48, 48))
    for i in range(len(X)):
        if i >= 19:
            break

        for x_i, img in enumerate(X[i]):
            axes = fig.add_subplot(20, 27, i * 27 + x_i + 1)
            axes.set_title(
                "{} ".format(
                    y[i]
                    # , clips[i].clip_id, clips[i].track_id, clips[i].frame_num,
                )
            )
            plt.axis("off")

            img = plt.imshow(tf.keras.preprocessing.image.array_to_img(img))
            img.set_cmap("hot")

    plt.savefig("testimage.png", bbox_inches="tight")
    plt.close(fig)
    print("saved image")
    # raise "DONE"
