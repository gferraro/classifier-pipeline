"""
Author: Matthew Aitchison
Date: December 2017

Dataset used for training a tensorflow model from track data.

Tracks are broken into segments.  Filtered, and then passed to the trainer using a weighted random sample.

"""

import logging
import math
import multiprocessing
import os
import queue
import random
import threading
import time
from bisect import bisect

import cv2
import dateutil
import numpy as np
import scipy.ndimage

# from load.clip import Clip
from ml_tools import tools
from ml_tools.trackdatabase import TrackDatabase
from track.region import Region

CPTV_FILE_WIDTH = 160
CPTV_FILE_HEIGHT = 120
FRAMES_PER_SECOND = 9

MASS_DIFF_PERCENT = 0.20
MAX_VELOCITY = 2
MAX_CROP_PERCENT = 0.3
MIN_CLARITY = 20
MIN_PERCENT = 60

RES_X = 160
RES_Y = 120
EDGE_PIXELS = 1


class TrackHeader:
    """ Header for track. """

    def __init__(
        self,
        clip_id,
        track_number,
        label,
        start_time,
        frames,
        duration,
        camera,
        location,
        score,
        track_bounds,
        frame_temp_median,
        frames_per_second,
        predictions,
        correct_prediction,
        frame_mass,
    ):
        self.predictions = predictions
        self.correct_prediction = correct_prediction
        self.filtered_stats = {"segment_mass": 0}
        # reference to clip this segment came from
        self.clip_id = clip_id
        # reference to track this segment came from
        self.track_number = track_number
        # label for this track
        self.label = label
        # date and time of the start of the track
        self.start_time = start_time
        # duration in seconds
        self.duration = duration
        # camera this track came from
        self.camera = camera

        self.location = location
        # score of track
        self.score = score
        # thermal reference point for each frame.
        self.frame_temp_median = frame_temp_median
        # tracking frame movements for each frame, array of tuples (x-vel, y-vel)
        self.frame_velocity = None
        # original tracking bounds
        self.track_bounds = track_bounds
        # what fraction of pixels are from out of bounds
        self.frame_crop = []
        self.frames = frames
        self.frames_per_second = frames_per_second
        self.calculate_velocity()
        self.calculate_frame_crop()
        self.important_frames = []
        self.important_predicted = 0
        self.frame_mass = frame_mass
        self.median_mass = np.median(frame_mass)
        self.mass_deviation = MASS_DIFF_PERCENT * np.amax(frame_mass)
        # print("mass max", np.amax(frame_mass), self.median_mass, np.amin(frame_mass))

    def get_sample_frame(self):
        if len(self.important_frames) == 0:
            return None
        f = self.important_frames[0]
        del self.important_frames[0]
        return f

    # trying to get only clear frames
    def set_important_frames(self, labels):
        crop_rectangle = tools.Rectangle(
            EDGE_PIXELS, EDGE_PIXELS, RES_X - 2 * EDGE_PIXELS, RES_Y - 2 * EDGE_PIXELS
        )
        frames = set()
        for i, vel in enumerate(self.frame_velocity):
            if vel[0] > MAX_VELOCITY or vel[1] > MAX_VELOCITY:
                continue
            if self.frame_crop[i] > MAX_CROP_PERCENT:
                continue
            rect = Region.region_from_array(self.track_bounds[i])
            rect.set_is_along_border(crop_rectangle)
            if rect.is_along_border:
                # print("rect on border")
                # print(self.track_bounds[i])
                continue
            if abs(self.frame_mass[i] - self.median_mass) > self.mass_deviation:
                # print("mass diff too big", self.frame_mass[i], self.median_mass)
                # print(self.frame_mass)
                continue
            frames.add(i)

        if self.predictions is not None:
            label_i = None
            fp_i = None
            if self.label in labels:
                label_i = list(labels).index(self.label)
            if "false-positive" in labels:
                fp_i = list(labels).index("false-positive")
            clear_frames = set()
            clear_frames = []
            best_preds = []
            incorrect_best = []

            for i, pred in enumerate(self.predictions):
                best = np.argsort(pred)
                if fp_i and best[-1] == fp_i:
                    continue

                clarity = best[-1] - best[-2]
                if clarity < MIN_CLARITY:
                    clear_frames.append((i, clarity))

                if label_i:
                    pred_percent = pred[label_i]
                    if pred_percent > MIN_PERCENT:
                        best_preds.append((i, pred_percent))

                if not self.correct_prediction:
                    if pred[best[-1]] > MIN_PERCENT:
                        incorrect_best.append((i, pred[best[-1]]))

            sorted(clear_frames, reverse=True, key=lambda frame: frame[1])
            sorted(best_preds, reverse=True, key=lambda frame: frame[1])
            sorted(incorrect_best, reverse=True, key=lambda frame: frame[1])

            pred_frames = set()
            pred_frames.update(f[0] for f in clear_frames[:2])
            pred_frames.update(f[0] for f in best_preds[:2])
            pred_frames.update(f[0] for f in incorrect_best[:2])
            pred_frames = frames.intersection(pred_frames)
            self.important_predicted = len(pred_frames)
            frames = list(frames - pred_frames)
            pred_frames = list(pred_frames)
            np.random.shuffle(pred_frames)

            np.random.shuffle(frames)
            pred_frames.extend(frames)
            self.important_frames = pred_frames
            return

        frames = list(frames)
        np.random.shuffle(frames)
        self.important_frames = list(frames)
        # print("setting important frames", frames)

    def calculate_frame_crop(self):
        # frames are always square, but bounding rect may not be, so to see how much we clipped I need to create a square
        # bounded rect and check it against frame size.
        self.frame_crop = []
        for rect in self.track_bounds:
            rect = tools.Rectangle.from_ltrb(*rect)
            rx, ry = rect.mid_x, rect.mid_y
            size = max(rect.width, rect.height)
            adjusted_rect = tools.Rectangle(rx - size / 2, ry - size / 2, size, size)
            self.frame_crop.append(
                get_cropped_fraction(adjusted_rect, CPTV_FILE_WIDTH, CPTV_FILE_HEIGHT)
            )

    def calculate_velocity(self):
        frame_center = [
            ((left + right) / 2, (top + bottom) / 2)
            for left, top, right, bottom in self.track_bounds
        ]
        self.frame_velocity = []
        prev = None
        for x, y in frame_center:
            if prev is None:
                self.frame_velocity.append((0.0, 0.0))
            else:
                self.frame_velocity.append((x - prev[0], y - prev[1]))
            prev = (x, y)

    @property
    def camera_id(self):
        """ Unique name of this track. """
        return "{}-{}".format(self.camera, self.location)

    @property
    def track_id(self):
        """ Unique name of this track. """
        return TrackHeader.get_name(self.clip_id, self.track_number)

    @property
    def bin_id(self):
        return self.track_id

    @property
    def weight(self):
        """ Returns total weight for all segments in this track"""
        return len(self.important_frames)

    @staticmethod
    def get_name(clip_id, track_number):
        return str(clip_id) + "-" + str(track_number)

    @staticmethod
    def from_meta(clip_id, clip_meta, track_meta, predictions):
        """ Creates a track header from given metadata. """
        # predictions = track_meta.get("predictions", None)
        correct_prediction = track_meta.get("correct_prediction", None)

        start_time = dateutil.parser.parse(track_meta["start_time"])
        end_time = dateutil.parser.parse(track_meta["end_time"])
        duration = (end_time - start_time).total_seconds()
        location = clip_meta.get("location")
        frames = track_meta["frames"]
        camera = clip_meta["device"]
        frames_per_second = clip_meta.get("frames_per_second", FRAMES_PER_SECOND)
        # get the reference levels from clip_meta and load them into the track.
        track_start_frame = track_meta["start_frame"]
        track_end_frame = track_meta["end_frame"]
        frame_temp_median = np.float32(
            clip_meta["frame_temp_median"][
                track_start_frame : frames + track_start_frame
            ]
        )

        bounds_history = track_meta["bounds_history"]

        header = TrackHeader(
            clip_id=int(clip_id),
            track_number=int(track_meta["id"]),
            label=track_meta["tag"],
            start_time=start_time,
            frames=frames,
            duration=duration,
            camera=camera,
            location=location,
            score=float(track_meta["score"]),
            track_bounds=np.asarray(bounds_history),
            frame_temp_median=frame_temp_median,
            frames_per_second=frames_per_second,
            predictions=predictions,
            correct_prediction=correct_prediction,
            frame_mass=track_meta["mass_history"],
        )
        return header

    def __repr__(self):
        return self.track_id


class Camera:
    def __init__(self, camera):
        self.label_to_bins = {}
        self.bins = {}
        self.label_frames = {}

        self.camera = camera
        self.tracks = 0
        self.bin_i = -1

    def label_tracks(self, label):
        return len(self.label_to_bins[label])

    def sample_frame(self, label):
        bins = self.label_to_bins[label]
        if len(bins) == 0:
            return None, None
        self.bin_i += 1
        self.bin_i = self.bin_i % len(bins)

        bin_id = bins[self.bin_i]
        track = self.bins[bin_id][0]
        f = track.get_sample_frame()
        if len(track.important_frames) == 0 or f is None:
            del bins[self.bin_i]
            del self.bins[bin_id]

        return track, f

    def label_frame_count(self, label, max_frames_per_track):
        if label not in self.label_to_bins:
            return 0
        bins = self.label_to_bins[label]
        frames = 0
        for bin in bins:
            tracks = self.bins[bin]
            for track in tracks:
                if max_frames_per_track:
                    frames += max(len(track.important_frames), max_frames_per_track)
                else:
                    frames += len(track.important_frames)

        return frames

    def add_track(self, track_header):

        if track_header.bin_id not in self.bins:
            self.bins[track_header.bin_id] = []

        if track_header.label not in self.label_to_bins:
            self.label_to_bins[track_header.label] = []
            self.label_frames[track_header.label] = 0

        if track_header.bin_id not in self.label_to_bins[track_header.label]:
            self.label_to_bins[track_header.label].append(track_header.bin_id)

        self.bins[track_header.bin_id].append(track_header)
        self.label_frames[track_header.label] += len(track_header.important_frames)
        self.tracks += 1


class FrameSample:
    def __init__(self, clip_id, track_id, frame_num):
        self.clip_id = clip_id
        self.track_id = track_id
        self.frame_num = frame_num


class FrameDataset:
    """
    Stores visit, clip, track, and segment information headers in memory, and allows track / segment streaming from
    disk.
    """

    def __init__(self, track_db: TrackDatabase, name="Dataset", config=None):
        # database holding track data
        self.db = track_db

        # name of this dataset
        self.name = name

        # list of our tracks
        self.camera_names = set()
        self.cameras_by_id = {}

        # writes the frame motion into the center of the optical flow channels
        self.encode_frame_offsets_in_flow = False

        self.frame_samples = []
        self.clips_to_samples = {}
        self.labels_to_samples = {}
        self.tracks_by_label = {}
        self.tracks_by_id = {}

        # list of label names
        self.labels = []

        # minimum mass of a segment frame for it to be included

        # dictionary used to apply label remapping during track load
        self.label_mapping = None

        # this allows manipulation of data (such as scaling) during the sampling stage.
        self.enable_augmentation = False
        self.preloader_queue = None
        self.preloader_threads = None
        self.preloader_stop_flag = False

        if config:

            self.banned_clips = config.build.banned_clips
            self.included_labels = config.labels
            self.clip_before_date = config.build.clip_end_date

        self.filtered_stats = {
            "confidence": 0,
            "trap": 0,
            "banned": 0,
            "date": 0,
            "tags": 0,
            "segment_mass": 0,
        }

    @property
    def rows(self):
        return len(self.tracks_by_id)

    def samples_for(self, label):
        return len(self.labels_to_samples.get(label, []))

    @property
    def frames(self):
        return len(self.frame_samples)

    def get_counts(self, label):
        """
        Gets number of examples for given label
        :label: label to check
        :return: (segments, tracks, bins, weight)
        """
        label_tracks = self.tracks_by_label.get(label, [])
        label_frames = self.labels_to_samples.get(label, [])
        tracks = len(label_tracks)
        return len(label_frames), tracks, tracks, 1

    def load_tracks(self, shuffle=False):
        """
        Loads track headers from track database with optional filter
        :return: [number of tracks added, total tracks].
        """
        counter = 0
        track_ids = self.db.get_all_track_ids()
        if shuffle:
            np.random.shuffle(track_ids)
        labels = self.db.get_labels()
        for clip_id, track_number in track_ids:
            if self.add_track(clip_id, track_number, labels):
                counter += 1
        return [counter, len(track_ids)]

    def add_track(self, clip_id, track_number, labels):
        """
        Creates segments for track and adds them to the dataset
        :param clip_id: id of tracks clip
        :param track_number: track number
        :param track_filter: if provided a function filter(clip_meta, track_meta) that returns true when a track should
                be ignored)
        :return: True if track was added, false if it was filtered out.
        :return:
        """

        # make sure we don't already have this track
        if TrackHeader.get_name(clip_id, track_number) in self.tracks_by_id:
            return False

        clip_meta = self.db.get_clip_meta(clip_id)
        track_meta = self.db.get_track_meta(clip_id, track_number)
        predictions = self.db.get_track_predictions(clip_id, track_number)
        if self.filter_track(clip_meta, track_meta):
            return False
        track_header = TrackHeader.from_meta(
            clip_id, clip_meta, track_meta, predictions
        )
        track_header.set_important_frames(labels)

        self.tracks_by_id[track_header.track_id] = track_header

        camera = self.cameras_by_id.setdefault(
            track_header.camera_id, Camera(track_header.camera_id)
        )
        self.camera_names.add(track_header.camera_id)
        camera.add_track(track_header)
        if track_header.label not in self.labels:
            self.labels.append(track_header.label)
        self.tracks_by_label.setdefault(track_header.label, set())
        self.tracks_by_label[track_header.label].add(track_header.track_id)
        return True

    def filter_track(self, clip_meta, track_meta):
        # some clips are banned for various reasons
        source = os.path.basename(clip_meta["filename"])
        if self.banned_clips and source in self.banned_clips:
            self.filtered_stats["banned"] += 1
            return True

        if track_meta["tag"] not in self.included_labels:
            self.filtered_stats["tags"] += 1
            return True

        if len(set(track_meta["track_tags"])) != len(set([track_meta["tag"]])):
            pass
            # self.filtered_stats["tags"] += 1
            # return True

        # filter by date
        if (
            self.clip_before_date
            and dateutil.parser.parse(clip_meta["start_time"]).date()
            > self.clip_before_date.date()
        ):
            self.filtered_stats["date"] += 1
            return True

        # always let the false-positives through as we need them even though they would normally
        # be filtered out.
        if track_meta["tag"] == "false-positive":
            return False

        # for some reason we get some records with a None confidence?
        if track_meta.get("confidence", 0.0) <= 0.6:
            self.filtered_stats["confidence"] += 1
            return True

        # remove tracks of trapped animals
        if (
            "trap" in clip_meta.get("event", "").lower()
            or "trap" in clip_meta.get("trap", "").lower()
        ):
            self.filtered_stats["trap"] += 1
            return True

        return False

    def add_tracks(self, tracks, max_frames_per_track=None):
        """
        Adds list of tracks to dataset
        :param tracks: list of TrackHeader
        :param track_filter: optional filter
        """
        result = 0
        for track in tracks:
            if self.add_track_header_frames(track, max_frames_per_track):
                result += 1
        return result

    def add_track_header_frame(self, track_header, frame):
        f = FrameSample(track_header.clip_id, track_header.track_number, frame)
        self.frame_samples.append(f)

        # this is just to print counts
        label_samples = self.labels_to_samples.setdefault(track_header.label, [])
        sample_index = self.clips_to_samples.setdefault(track_header.clip_id, [])

        label_samples.append(len(self.frame_samples) - 1)
        sample_index.append(len(self.frame_samples) - 1)

        self.tracks_by_label.setdefault(track_header.label, set())
        self.tracks_by_label[track_header.label].add(track_header.track_id)

        if track_header.label not in self.labels:
            self.labels.append(track_header.label)

        self.tracks_by_id[track_header.track_id] = track_header
        self.camera_names.add(track_header.camera_id)
        return True

    def fetch_frame(self, frame_sample):
        data = self.db.get_track(
            frame_sample.clip_id,
            frame_sample.track_id,
            frame_sample.frame_num,
            frame_sample.frame_num + 1,
        )
        label = self.db.get_tag(frame_sample.clip_id, frame_sample.track_id)
        return data[0], label

    #
    # def relabel(self, keep=[], wildcardlabel):
    #     new_samples = []
    #     tracks_by_id = {}
    #     tracks = []
    #
    #
    #     for key, value in self.labels_to_samples.items():
    #         if label in keep:
    #             continue
    #         track_ids = self.tracks_by_label.setdefault(wildcardlabel,set())
    #
    #         samples = self.labels_to_samples.setdefault(wildcardlabel,[])
    #         samples.extend(value)
    #         for i in value:
    #             track_id = TrackHeader.get_name(
    #                 self.frame_samples[i].clip_id, self.frame_samples[i].track_id
    #             )
    #
    #             track = self.tracks_by_id[track_id]
    #             track_ids.add(track_id)
    #             tracks_by_id[track_id] = track
    #             new_samples.append(self.frame_samples[i])
    #
    #     self.tracks_by_id = tracks_by_id
    #     self.frame_samples = new_samples

    def rebalance(self, label_cap=1000, exclude=[]):
        new_samples = []
        tracks_by_id = {}
        tracks = []
        wallaby_count = len(self.labels_to_samples["wallaby"])
        print("wallaby count", wallaby_count)
        cap_else = int(wallaby_count * 2 / (len(self.labels) - 1))
        for key, value in self.labels_to_samples.items():
            track_ids = set()
            self.tracks_by_label[key] = track_ids
            if key in exclude:
                self.labels.remove(key)
                continue
            if key != "wallaby":
                label_cap = cap_else
                print("set cap to", cap_else)
            else:
                continue
                # label_cap = None
            print("set cap to", cap_else, key)

            np.random.shuffle(value)
            if label_cap:
                value = value[:label_cap]
            self.labels_to_samples[key] = value
            for i in value:
                track_id = TrackHeader.get_name(
                    self.frame_samples[i].clip_id, self.frame_samples[i].track_id
                )

                track = self.tracks_by_id[track_id]
                track_ids.add(track_id)
                tracks_by_id[track_id] = track
                new_samples.append(self.frame_samples[i])

        self.tracks_by_id = tracks_by_id
        self.frame_samples = new_samples

    def get_label_frames_count(self, label):
        """ Returns the total important frames for all tracks of given class. """
        return len(self.labels_to_samples.get(label, []))


def get_cropped_fraction(region: tools.Rectangle, width, height):
    """ Returns the fraction regions mass outside the rect ((0,0), (width, height)"""
    bounds = tools.Rectangle(0, 0, width - 1, height - 1)
    return 1 - (bounds.overlap_area(region) / region.area)


def dataset_db_path(config):
    return os.path.join(config.tracks_folder, "datasets.dat")


class TrackChannels:
    """ Indexes to channels in track. """

    thermal = 0
    filtered = 1
    flow_h = 2
    flow_v = 3
    mask = 4
