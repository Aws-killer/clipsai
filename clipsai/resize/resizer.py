"""
Resizing/cropping a media file to a different aspect ratio

Notes
-----
- ROI is "region of interest"
"""
# standard library imports
import logging
import math
# current package imports
from .crops import Crops
from .exceptions import ResizerError
from .img_proc import calc_img_bytes
from .rect import Rect
from .segment import Segment
from .vid_proc import extract_frames

# local package imports
from clipsai.media.editor import MediaEditor
from clipsai.media.video_file import VideoFile
from clipsai.utils import pytorch
from clipsai.utils.conversions import bytes_to_gibibytes

# 3rd party imports
import cv2
from facenet_pytorch import MTCNN
import mediapipe as mp
import numpy as np
from sklearn.cluster import KMeans
import torch


class Resizer:
    """
    A class for calculating the initial coordinates for resizing by using
    segmentation and face detection.
    """

    def __init__(
        self,
        face_detect_margin: int = 20,
        face_detect_post_process: bool = False,
        device: str = None,
    ) -> None:
        """
        Initializes the Resizer with specific configurations for face
        detection. This class uses FaceNet for detecting faces and MediaPipe for
        analyzing mouth to aspect ratio to determine whose speaking within video frames.

        Parameters
        ----------
        face_detect_margin: int, optional
            The margin around detected faces, specified in pixels. Increasing this
            value results in a larger area around each detected face being included.
            Default is 20 pixels.
        face_detect_post_process: bool, optional
            Determines whether to apply post-processing on the detected faces. Setting
            this to False prevents normalization of output images, making them appear
            more natural to the human eye. Default is False (no post-processing).
        device: str, optional
            PyTorch device to perform computations on. Ex: 'cpu', 'cuda'. Default is
            None (auto detects the correct device)
        """
        if device is None:
            device = pytorch.get_compute_device()
        pytorch.assert_compute_device_available(device)
        logging.debug("FaceNet using device: {}".format(device))

        self._face_detector = MTCNN(
            margin=face_detect_margin,
            post_process=face_detect_post_process,
            device=device,
        )
        # media pipe automatically uses gpu if available
        self._face_mesher = mp.solutions.face_mesh.FaceMesh()
        self._media_editor = MediaEditor()

    def resize(
        self,
        video_file: VideoFile,
        speaker_segments: list[dict],
        scene_changes: list[float],
        aspect_ratio: tuple = (9, 16),
        samples_per_segment: int = 13,
        face_detect_width: int = 960,
        n_face_detect_batches: int = 8,
        scene_merge_threshold: float = 0.25,
    ) -> Crops:
        """
        Calculates the coordinates to resize the video to for different
        segments given the diarized speaker segments and the desired aspect
        ratio.

        Parameters
        ----------
        video_file: VideoFile
            The video file to resize
        speaker_segments: list[dict]
            speakers: list[int]
                list of speakers (represented by int) talking in the segment
            start_time: float
                start time of the segment in seconds
            end_time: float
                end time of the segment in seconds
        scene_changes: list[float]
            List of scene change times in seconds
        aspect_ratio: tuple[int, int]
            The (width,height) aspect ratio to resize the video to
        samples_per_segment: int
            Number of frames to sample per segment for face detection.
        face_detect_width: int
            The width to use for face detection
        n_face_detect_batches: int
            Number of batches for GPU face detection in a video file
        scene_merge_threshold: float
            The threshold in seconds for merging scene changes with speaker segments.
            Scene changes within this threshold of a segment's start or end time will
            cause the segment to be adjusted.

        Returns
        -------
        Crops
            the resized speaker segments
        """
        logging.debug(
            "Video Resolution: {}x{}".format(
                video_file.get_width_pixels(), video_file.get_height_pixels()
            )
        )
        # calculate resize dimensions
        resize_width, resize_height = self._calc_resize_width_and_height_pixels(
            original_width_pixels=video_file.get_width_pixels(),
            original_height_pixels=video_file.get_height_pixels(),
            resize_aspect_ratio=aspect_ratio,
        )

        logging.debug(
            "Merging {} speaker segments with {} scene changes.".format(
                len(speaker_segments), len(scene_changes)
            )
        )
        segments = self._merge_scene_change_and_speaker_segments(
            speaker_segments, scene_changes, scene_merge_threshold
        )
        logging.debug("Video has {} distinct segments.".format(len(segments)))

        logging.debug("Determining the first second with a face for each segment.")
        segments = self._find_first_sec_with_face_for_each_segment(
            segments, video_file, face_detect_width, n_face_detect_batches
        )

        logging.debug(
            "Determining the region of interest for {} segments.".format(len(segments))
        )
        segments = self._add_x_y_coords_to_each_segment(
            segments,
            video_file,
            resize_width,
            resize_height,
            samples_per_segment,
            face_detect_width,
            n_face_detect_batches,
        )

        logging.debug("Merging identical segments together.")
        unmerge_segments_length = len(segments)
        segments = self._merge_identical_segments(segments, video_file)
        logging.debug(
            "Merged {} identical segments.".format(
                unmerge_segments_length - len(segments)
            )
        )
        crop_segments = []
        for segment in segments:
            if segment['crop_type'] == 'single':
                crop_segments.append(
                    Segment(
                        speakers=segment["speakers"],
                        start_time=segment["start_time"],
                        end_time=segment["end_time"],
                        x=segment["x"],
                        y=segment["y"],
                    )
                )
            elif segment['crop_type'] == 'split':
                # For split-screen segments, we'll create a Segment for each crop
                for crop in segment['crops']:
                    crop_segments.append(
                        Segment(
                            speakers=segment["speakers"],
                            start_time=segment["start_time"],
                            end_time=segment["end_time"],
                            x=crop["x"],
                            y=crop["y"],
                            width=crop["width"],
                            height=crop["height"],
                            target_x=crop["target_x"],
                            target_y=crop["target_y"],
                        )
                    )

        crops = Crops(
            original_width=video_file.get_width_pixels(),
            original_height=video_file.get_height_pixels(),
            crop_width=resize_width,
            crop_height=resize_height,
            segments=crop_segments,
        )

        return crops




    def _calc_resize_width_and_height_pixels(
        self,
        original_width_pixels: int,
        original_height_pixels: int,
        resize_aspect_ratio: tuple[int, int],
    ) -> tuple[int, int]:
        """
        Calculate the number of pixels along the width and height to resize the video
        to based on the desired aspect ratio.

        Parameters
        ----------
        original_pixels_width: int
            Number of pixels along the width of the original video.
        original_pixels_height: int
            Number of pixels along the height of the original video
        resize_aspect_ratio: tuple[int, int]
            The width:height aspect ratio to resize the video to

        Returns
        -------
        tuple[int, int]
            The number of pixels along the width and height to resize the video to
        """
        resize_ar_width, resize_ar_height = resize_aspect_ratio
        desired_aspect_ratio = resize_ar_width / resize_ar_height
        original_aspect_ratio = original_width_pixels / original_height_pixels

        # original aspect ratio is wider than desired aspect ratio
        if original_aspect_ratio > desired_aspect_ratio:
            resize_height_pixels = original_height_pixels
            resize_width_pixels = int(
                resize_height_pixels * resize_ar_width / resize_ar_height
            )
        # original aspect ratio is taller than desired aspect ratio
        else:
            resize_width_pixels = original_width_pixels
            resize_height_pixels = int(
                resize_width_pixels * resize_ar_height / resize_ar_width
            )

        return resize_width_pixels, resize_height_pixels

    def _merge_scene_change_and_speaker_segments(
        self,
        speaker_segments: list[dict],
        scene_changes: list[float],
        scene_merge_threshold: float,
    ) -> list[dict]:
        """
        Merge scene change segments with speaker segments based on a specified
        threshold.

        Parameters
        ----------
        speaker_segments: list[dict]
            speakers: list[int]
                list of speaker numbers for the speakers talking in the segment
            start_time: float
                start time of the segment in seconds
            end_time: float
                end time of the segment in seconds
        scene_changes: list[float]
            List of scene change times in seconds.
        scene_merge_threshold: float
            The threshold in seconds for merging scene changes with speaker segments.
            Scene changes within this threshold of a segment's start or end time will
            cause the segment to be adjusted.

        Returns
        -------
        updated_speaker_segments: list[dict]
            speakers: list[int]
                list of speaker numbers for the speakers talking in the segment
            start_time: float
                start time of the segment in seconds
            end_time: float
                end time of the segment in seconds
        """
        segments_idx = 0
        for scene_change_sec in scene_changes:
            segment = speaker_segments[segments_idx]
            while scene_change_sec > (segment["end_time"]):
                segments_idx += 1
                segment = speaker_segments[segments_idx]
            # scene change is close to speaker segment end -> merge the two
            if 0 < (segment["end_time"] - scene_change_sec) < scene_merge_threshold:
                segment["end_time"] = scene_change_sec
                if segments_idx == len(speaker_segments) - 1:
                    continue
                next_segment = speaker_segments[segments_idx + 1]
                next_segment["start_time"] = scene_change_sec
                continue
            # scene change is close to speaker segment start -> merge the two
            if 0 < (scene_change_sec - segment["start_time"]) < scene_merge_threshold:
                segment["start_time"] = scene_change_sec
                if segments_idx == 0:
                    continue
                prev_segment = speaker_segments[segments_idx - 1]
                prev_segment["end_time"] = scene_change_sec
                continue
            # scene change already exists
            if scene_change_sec == segment["end_time"]:
                continue
            # add scene change to segments
            new_segment = {
                "start_time": scene_change_sec,
                "speakers": segment["speakers"],
                "end_time": segment["end_time"],
            }
            segment["end_time"] = scene_change_sec
            speaker_segments = (
                speaker_segments[: segments_idx + 1]
                + [new_segment]
                + speaker_segments[segments_idx + 1 :]
            )

        return speaker_segments

    def _find_first_sec_with_face_for_each_segment(
        self,
        segments: list[dict],
        video_file: VideoFile,
        face_detect_width: int,
        n_face_detect_batches: int,
    ) -> list[dict]:
        """
        Find the first frame in a segment with a face.

        Parameters
        ----------
        segments: list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                start_time: float
                    start time of the segment in seconds
                end_time: float
                    end time of the segment in seconds
        video_file: VideoFile
            The video file to analyze.
        n_face_detect_batches: int
            The number of batches to use for identifyinng faces from a video file

        Returns
        -------
        list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                start_time: float
                    start time of the segment in seconds
                end_time: float
                    end time of the segment in seconds
                first_face_sec: float
                    the first second in the segment with a face
                found_face: bool
                    whether or not a face was found in the segment
        """
        for segment in segments:
            start_time = segment["start_time"]
            end_time = segment["end_time"]
            # start looking for faces an eighth of the way through the segment
            segment["first_face_sec"] = start_time + (end_time - start_time) / 8
            segment["found_face"] = False
            segment["is_analyzed"] = False

        batch_period = 1  # interval length to sample each segment at each iteration
        sample_period = 1  # interval between consecutive samples
        analyzed_segments = 0
        while analyzed_segments < len(segments):
            # select times to detect faces from
            detect_secs = []
            for segment in segments:
                if segment["is_analyzed"] is True:
                    continue
                segment_secs_left = segment["end_time"] - segment["first_face_sec"]
                num_samples = min(batch_period, segment_secs_left) // sample_period
                num_samples = max(1, int(num_samples))
                segment["num_samples"] = num_samples
                for i in range(num_samples):
                    detect_secs.append(segment["first_face_sec"] + i * sample_period)

            # detect faces
            n_batches = self._calc_n_batches(
                video_file=video_file,
                num_frames=len(detect_secs),
                face_detect_width=face_detect_width,
                n_face_detect_batches=n_face_detect_batches,
            )
            frames_per_batch = int(len(detect_secs) // n_batches + 1)
            face_detections = []
            for i in range(n_batches):
                frames = extract_frames(
                    video_file,
                    detect_secs[
                        i
                        * frames_per_batch : min(
                            (i + 1) * frames_per_batch, len(detect_secs)
                        )
                    ],
                )
                face_detections += self._detect_faces(frames, face_detect_width)

            # check if any faces were found for each segment
            idx = 0
            for segment in segments:
                # segment already analyzed
                if segment["is_analyzed"] is True:
                    continue
                segment_idx = idx
                # check if any faces were found
                for _ in range(segment["num_samples"]):
                    faces = face_detections[idx]
                    if faces is not None:
                        segment["found_face"] = True
                        break
                    segment["first_face_sec"] += sample_period
                    idx += 1
                # update segment analyzation status
                is_analyzed = (
                    segment["found_face"] is True
                    or segment["first_face_sec"] >= segment["end_time"] - 0.25
                )
                if is_analyzed:
                    segment["is_analyzed"] = True
                    analyzed_segments += 1
                idx = segment_idx + segment["num_samples"]

            # increase period for next iteration
            batch_period = (batch_period + 3) * 2

        for segment in segments:
            del segment["num_samples"]
            del segment["is_analyzed"]

        return segments

    def _calc_n_batches(
        self,
        video_file: VideoFile,
        num_frames: int,
        face_detect_width: int,
        n_face_detect_batches: int,
    ) -> int:
        """
        Calculate the number of batches to use for extracting frames from a video file
        and detecting the face in each frame.

        Parameters
        ----------
        video_file: VideoFile
            The video file to analyze.
        num_frames: int
            The number of frames to analyze.
        face_detect_width: int
            The width to use for face detection.
        n_face_detect_batches: int
            Number of batches for GPU face detection in a video file.

        Returns
        -------
        int
            The number of batches to use.
        """
        # calculate memory needed to extract frames to CPU
        vid_height = video_file.get_height_pixels()
        vid_width = video_file.get_width_pixels()
        num_color_channels = 3
        bytes_per_frame = calc_img_bytes(vid_height, vid_width, num_color_channels)
        total_extract_bytes = num_frames * bytes_per_frame
        logging.debug(
            "Need {:.3f} GiB to extract (at most) {} frames".format(
                bytes_to_gibibytes(total_extract_bytes), num_frames
            )
        )

        # calculate memory needed to detect faces -> could be CPU or GPU
        downsample_factor = max(vid_width / face_detect_width, 1)
        face_detect_height = int(vid_height // downsample_factor)
        logging.debug(
            "Face detection dimensions: {}x{}".format(
                face_detect_height, face_detect_width
            )
        )
        bytes_per_frame = calc_img_bytes(
            face_detect_height, face_detect_width, num_color_channels
        )
        total_face_detect_bytes = num_frames * bytes_per_frame
        logging.debug(
            "Need {:.3f} GiB to detect faces from (at most) {} frames".format(
                bytes_to_gibibytes(total_face_detect_bytes), num_frames
            )
        )

        # calculate number of batches to use
        free_cpu_memory = pytorch.get_free_cpu_memory()
        if torch.cuda.is_available():
            n_extract_batches = int((total_extract_bytes // free_cpu_memory) + 1)
        else:
            total_extract_bytes += total_face_detect_bytes
            n_extract_batches = int((total_extract_bytes // free_cpu_memory) + 1)
            n_face_detect_batches = 0

        n_batches = int(max(n_extract_batches, n_face_detect_batches))
        cpu_mem_per_batch = bytes_to_gibibytes(total_extract_bytes // n_batches)
        if n_face_detect_batches == 0:
            gpu_mem_per_batch = 0
        else:
            gpu_mem_per_batch = bytes_to_gibibytes(total_face_detect_bytes // n_batches)
        logging.debug(
            "Using {} batches to extract and detect frames. Need {:.3f} GiB of CPU "
            "memory per batch and {:.3f} GiB of GPU memory per batch".format(
                n_batches,
                cpu_mem_per_batch,
                gpu_mem_per_batch,
            )
        )
        return n_batches

    def _detect_faces(
        self,
        frames: list[np.ndarray],
        face_detect_width: int,
    ) -> list[np.ndarray]:
        """
        Detect faces in a list of frames.

        Parameters
        ----------
        frames: list[np.ndarray]
            The frames to detect faces in.
        face_detect_width: int
            The width to use for face detection.

        Returns
        -------
        list[np.ndarray]
            The face detections for each frame.
        """
        if len(frames) == 0:
            logging.debug("No frames to detect faces in.")
            return []

        # resize the frames
        logging.debug("Detecting faces in {} frames.".format(len(frames)))
        downsample_factor = max(frames[0].shape[1] / face_detect_width, 1)
        detect_height = int(frames[0].shape[0] / downsample_factor)
        resized_frames = []
        for frame in frames:
            resized_frame = cv2.resize(frame, (face_detect_width, detect_height))
            if torch.cuda.is_available():
                resized_frame = torch.from_numpy(resized_frame).to(
                    device="cuda", dtype=torch.uint8
                )
            resized_frames.append(resized_frame)

        # detect faces in batches
        if torch.cuda.is_available():
            resized_frames = torch.stack(resized_frames)
        detections, _ = self._face_detector.detect(resized_frames)

        # detections are returned as numpy arrays regardless
        face_detections = []
        for detection in detections:
            if detection is not None:
                detection[detection < 0] = 0
                detection = (detection * downsample_factor).astype(np.int16)
            face_detections.append(detection)

        logging.debug("Detected faces in {} frames.".format(len(face_detections)))
        return face_detections

    def _add_x_y_coords_to_each_segment(
        self,
        segments: list[dict],
        video_file: VideoFile,
        resize_width: int,
        resize_height: int,
        samples_per_segment: int,
        face_detect_width: int,
        n_face_detect_batches: int,
    ) -> list[dict]:
        """
        Add the x and y coordinates to resize each segment to.

        Parameters
        ----------
        segments: list[dict]
            speakers: list[int]
                list of speaker numbers for the speakers talking in the segment
            start_time: float
                start time of the segment in seconds
            end_time: float
                end time of the segment in seconds
            first_face_sec: float
                the first second in the segment with a face
            found_face: bool
                whether or not a face was found in the segment
        video_file: VideoFile
            The video file to analyze.
        resize_width: int
            The width to resize the video to.
        resize_height: int
            The height to resize the video to.
        samples_per_segment: int
            Number of samples to take per segment for face detection.
        face_detect_width: int
            Width to resize the frames to for face detection.
        n_face_detect_batches: int
            Number of batches to process for face detection.


        Returns
        -------
        updated_segments: list[dict]
            speakers: list[int]
                list of speaker numbers for the speakers talking in the segment
            start_time: float
                start time of the segment in seconds
            end_time: float
                end time of the segment in seconds
            x: int
                x-coordinate of the top left corner of the resized segment
            y: int
                y-coordinate of the top left corner of the resized segment
        """
        num_segments = len(segments)
        num_frames = num_segments * samples_per_segment
        n_batches = self._calc_n_batches(
            video_file, num_frames, face_detect_width, n_face_detect_batches
        )
        segments_per_batch = int(num_segments // n_batches + 1)
        segments_with_xy_coords = []
        for i in range(n_batches):
            logging.debug("Analyzing batch {} of {}.".format(i, n_batches))
            cur_segments = segments[
                i
                * segments_per_batch : min((i + 1) * segments_per_batch, len(segments))
            ]
            if len(cur_segments) == 0:
                logging.debug("No segments left to analyze. (Batch {})".format(i))
                break
            segments_with_xy_coords += self._add_x_y_coords_to_each_segment_batch(
                segments=cur_segments,
                video_file=video_file,
                resize_width=resize_width,
                resize_height=resize_height,
                samples_per_segment=samples_per_segment,
                face_detect_width=face_detect_width,
            )
        return segments_with_xy_coords

    def _add_x_y_coords_to_each_segment_batch(
        self,
        segments: list[dict],
        video_file: VideoFile,
        resize_width: int,
        resize_height: int,
        samples_per_segment: int,
        face_detect_width: int,
    ) -> list[dict]:
        """
        Add the x and y coordinates to resize each segment to for a given batch.

        Parameters
        ----------
        segments: list[dict]
            speakers: list[int]
                list of speaker numbers for the speakers talking in the segment
            start_time: float
                start time of the segment in seconds
            end_time: float
                end time of the segment in seconds
            first_face_sec: float
                the first second in the segment with a face
            found_face: bool
                whether or not a face was found in the segment
        video_file: VideoFile
            The video file to analyze.
        resize_width: int
            The width to resize the video to.
        resize_height: int
            The height to resize the video to.
        samples_per_segment: int
            Number of samples to take per segment for analyzing face locations.
        face_detect_width: int
            Width to which the video frames are resized for face detection.

        Returns
        -------
        updated_segments: list[dict]
            speakers: list[int]
                list of speaker numbers for the speakers talking in the segment
            start_time: float
                start time of the segment in seconds
            end_time: float
                end time of the segment in seconds
            x: int
                x-coordinate of the top left corner of the resized segment
            y: int
                y-coordinate of the top left corner of the resized segment
        """
        fps = video_file.get_frame_rate()

        # define frames to analyze from each segment
        detect_secs = []
        for segment in segments:
            if segment["found_face"] is False:
                continue
            # define interval over which to analyze faces
            end_time = segment["end_time"]
            first_face_sec = segment["first_face_sec"]
            analyze_end_time = end_time - (end_time - first_face_sec) / 8
            # get sample locations
            frames_left = int((analyze_end_time - first_face_sec) * fps + 1)
            num_samples = min(frames_left, samples_per_segment)
            segment["num_samples"] = num_samples
            # add first face, sample the rest
            detect_secs.append(first_face_sec)
            sample_frames = np.sort(
                np.random.choice(range(1, frames_left), num_samples - 1, replace=False)
            )
            for sample_frame in sample_frames:
                detect_secs.append(first_face_sec + sample_frame / fps)

        # detect faces from each segment
        logging.debug("Extracting {} frames".format(len(detect_secs)))
        frames = extract_frames(video_file, detect_secs)
        logging.debug("Extracted {} frames".format(len(detect_secs)))
        face_detections = self._detect_faces(frames, face_detect_width)

        logging.debug("Calculating ROI for {} segments.".format(len(segments)))


        logging.debug("Calculating ROI for {} segments.".format(len(segments)))
        # find roi for each segment
        idx = 0
        for segment in segments:
            # find segment roi
            if segment["found_face"] is True:
                rois = self._calc_segment_rois(
                    frames=frames[idx : idx + segment["num_samples"]],
                    face_detections=face_detections[idx : idx + segment["num_samples"]],
                )
                idx += segment["num_samples"]
                del segment["num_samples"]
            else:
                logging.debug("Using default ROI for segment {}".format(segment))
                rois = [Rect(
                    x=(video_file.get_width_pixels()) // 4,
                    y=(video_file.get_height_pixels()) // 4,
                    width=(video_file.get_width_pixels()) // 2,
                    height=(video_file.get_height_pixels()) // 2,
                )]
            del segment["found_face"]
            del segment["first_face_sec"]

            # add crop coordinates to segment
            if len(rois) == 1:
                crop = self._calc_crop(rois[0], resize_width, resize_height)
                segment["x"] = int(crop.x)
                segment["y"] = int(crop.y)
                segment["crop_type"] = "single"
            elif len(rois) > 1:
                segment["crops"] = self._calc_split_screen_crops(rois, resize_width, resize_height)
                segment["crop_type"] = "split"
            else:
                # Fallback to center crop if no ROIs found
                segment["x"] = (video_file.get_width_pixels() - resize_width) // 2
                segment["y"] = (video_file.get_height_pixels() - resize_height) // 2
                segment["crop_type"] = "single"

        logging.debug("Calculated ROI for {} segments.".format(len(segments)))

        return segments

    def _calc_segment_rois(
        self,
        frames: list[np.ndarray],
        face_detections: list[np.ndarray],
    ) -> list[Rect]:
        # Similar to _calc_segment_roi, but returns a list of ROIs
        rois = []
        for face_detection in face_detections:
            if face_detection is not None:
                for bbox in face_detection:
                    x1, y1, x2, y2 = bbox[:4]
                    rois.append(Rect(x1, y1, x2 - x1, y2 - y1))
        
        # Merge overlapping ROIs
        return self.merge_rois(rois)

    def merge_rois(self, rois: list[Rect], overlap_threshold: float = 0.5) -> list[Rect]:
        merged = []
        while rois:
            roi = rois.pop(0)
            i = 0
            while i < len(rois):
                if self.iou(roi, rois[i]) > overlap_threshold:
                    roi = self.union_rois(roi, rois.pop(i))
                else:
                    i += 1
            merged.append(roi)
        return merged

    @staticmethod
    def iou(rect1: Rect, rect2: Rect) -> float:
        x1 = max(rect1.x, rect2.x)
        y1 = max(rect1.y, rect2.y)
        x2 = min(rect1.x + rect1.width, rect2.x + rect2.width)
        y2 = min(rect1.y + rect1.height, rect2.y + rect2.height)
        
        intersection = max(0, x2 - x1) * max(0, y2 - y1)
        union = rect1.width * rect1.height + rect2.width * rect2.height - intersection
        
        return intersection / union if union > 0 else 0

    @staticmethod
    def union_rois(rect1: Rect, rect2: Rect) -> Rect:
        x = min(rect1.x, rect2.x)
        y = min(rect1.y, rect2.y)
        width = max(rect1.x + rect1.width, rect2.x + rect2.width) - x
        height = max(rect1.y + rect1.height, rect2.y + rect2.height) - y
        return Rect(x, y, width, height)

    def _calc_split_screen_crops(
        self,
        rois: list[Rect],
        resize_width: int,
        resize_height: int,
    ) -> list[dict]:
        crops = []
        n_rois = len(rois)
        
        if n_rois == 2:
            # For two ROIs, split the screen vertically
            for i, roi in enumerate(rois):
                crop = self._calc_crop(roi, resize_width, resize_height // 2)
                crops.append({
                    "x": int(crop.x),
                    "y": int(crop.y),
                    "width": resize_width,
                    "height": resize_height // 2,
                    "target_y": i * (resize_height // 2)
                })
        elif n_rois > 2:
            # For more than two ROIs, use a grid layout
            grid_size = math.ceil(math.sqrt(n_rois))
            cell_width = resize_width // grid_size
            cell_height = resize_height // grid_size
            
            for i, roi in enumerate(rois):
                crop = self._calc_crop(roi, cell_width, cell_height)
                crops.append({
                    "x": int(crop.x),
                    "y": int(crop.y),
                    "width": cell_width,
                    "height": cell_height,
                    "target_x": (i % grid_size) * cell_width,
                    "target_y": (i // grid_size) * cell_height
                })
        
        return crops

    def _calc_segment_roi(
        self,
        frames: list[np.ndarray],
        face_detections: list[np.ndarray],
    ) -> Rect:
        """
        Find the region of interest (ROI) for a given segment.

        Parameters
        ----------
        frames: np.ndarray
            The frames to analyze.
        face_detections: np.ndarray
            The face detection outputs for each frame

        Returns
        -------
        Rect
            The region of interest (ROI) for the segment.
        """
        segment_roi = None

        # preprocessing for kmeans
        bounding_boxes: list[np.ndarray] = []
        k = 0
        for face_detection in face_detections:
            if face_detection is None:
                continue
            k = max(k, len(face_detection))
            for bounding_box in face_detection:
                bounding_boxes.append(bounding_box)

        # no faces detected
        if k == 0:
            raise ResizerError("No faces detected in segment.")
        bounding_boxes = np.stack(bounding_boxes)

        # single face detected
        if k == 1:
            box = np.mean(bounding_boxes, axis=0).astype(np.int16)
            x1, y1, x2, y2 = box
            segment_roi = Rect(x1, y1, x2 - x1, y2 - y1)
            return segment_roi

        # use kmeans to group the same bounding boxes together
        kmeans = KMeans(n_clusters=k, init="k-means++", n_init=2, random_state=0).fit(
            bounding_boxes
        )
        bounding_box_labels = kmeans.labels_
        bounding_box_groups: list[list[dict]] = [[] for _ in range(k)]
        kmeans_idx = 0
        for i, face_detection in enumerate(face_detections):
            if face_detection is None:
                continue
            for bounding_box in face_detection:
                assert np.sum(bounding_box < 0) == 0
                bounding_box_label = bounding_box_labels[kmeans_idx]
                bounding_box_groups[bounding_box_label].append(
                    {"bounding_box": bounding_box, "frame": i}
                )
                kmeans_idx += 1

        # find the face who's mouth moves the most
        max_mouth_movement = 0
        for bounding_box_group in bounding_box_groups:
            mouth_movement, roi = self._calc_mouth_movement(bounding_box_group, frames)
            if mouth_movement > max_mouth_movement:
                max_mouth_movement = mouth_movement
                segment_roi = roi

        # no mouth movement detected -> choose face with the most frames
        if segment_roi is None:
            logging.debug("No mouth movement detected for segment.")
            max_frames = 0
            for bounding_box_group in bounding_box_groups:
                if len(bounding_box_group) > max_frames:
                    max_frames = len(bounding_box_group)
                    avg_box = np.array([0, 0, 0, 0])
                    for bounding_box_data in bounding_box_group:
                        avg_box += bounding_box_data["bounding_box"]
                    avg_box = avg_box / len(bounding_box_group)
                    avg_box = avg_box.astype(np.int16)
                    segment_roi = Rect(
                        avg_box[0],
                        avg_box[1],
                        avg_box[2] - avg_box[0],
                        avg_box[3] - avg_box[1],
                    )

        return segment_roi

    def _calc_mouth_movement(
        self,
        bounding_box_group: list[dict[np.ndarray, int]],
        frames: list[np.ndarray],
    ) -> tuple[float, Rect]:
        """
        Calculates the mouth movement for a group of faces. These faces are assumed to
        all be the same person in different frames of the source video. Further, the
        frames are assumed to be in order of occurrence (earlier frames first).

        Parameters
        ----------
        bounding_box_group: list[dict[np.ndarray, int]]
            The faces to analyze. A list of dictionaries, each with the following keys:
                bounding_box: np.ndarray
                    The bounding box of the face to analyze. The array contains four
                    values: [x1, y1, x2, y2]
                frame: int
                    The frame the bounding box of the face is associated with.
        frames: list[np.ndarray]
            The frames to analyze.

        Returns
        -------
        float
            The mouth movement of the faces across the frames.
        """
        mouth_movement = 0
        roi = Rect(0, 0, 0, 0)
        prev_mar = None
        mouth_movement = 0

        for bounding_box_data in bounding_box_group:
            # roi
            box = bounding_box_data["bounding_box"]
            x1, y1, x2, y2 = box[0], box[1], box[2], box[3]
            # sum all roi's, average after loop
            roi += Rect(x1, y1, x2 - x1, y2 - y1)
            frame = frames[bounding_box_data["frame"]]
            face = frame[y1:y2, x1:x2, :]

            # mouth movement
            mar = self._calc_mouth_aspect_ratio(face)
            if mar is None:
                continue
            if prev_mar is None:
                prev_mar = mar
                continue
            mouth_movement += abs(mar - prev_mar)
            prev_mar = mar

        return mouth_movement, roi / len(bounding_box_group)

    def _calc_mouth_aspect_ratio(self, face: np.ndarray) -> float:
        """
        Calculate the mouth aspect ratio using dlib shape predictor.

        Parameters
        ----------
        face: np.ndarray
            Pytorch array of a face

        Returns
        -------
        mar: float
            The mouth aspect ratio.
        """
        results = self._face_mesher.process(face)
        if results.multi_face_landmarks is None:
            return None

        landmarks = []
        for landmark in results.multi_face_landmarks[0].landmark:
            landmarks.append([landmark.x, landmark.y])
        landmarks = np.array(landmarks)
        landmarks[:, 0] *= face.shape[1]
        landmarks[:, 1] *= face.shape[0]

        # inner lip
        upper_lip = landmarks[[95, 88, 178, 87, 14, 317, 402, 318, 324], :]
        lower_lip = landmarks[[191, 80, 81, 82, 13, 312, 311, 310, 415], :]
        avg_mouth_height = np.mean(np.abs(upper_lip - lower_lip))
        mouth_width = np.sum(np.abs(landmarks[[308], :] - landmarks[[78], :]))
        mar = avg_mouth_height / mouth_width

        return mar

    def _calc_crop(
        self,
        roi: Rect,
        resize_width: int,
        resize_height: int,
    ) -> Rect:
        """
        Calculate the crop given the ROI location.

        Parameters
        ----------
        roi: Rect
            The rectangle containing the region of interest (ROI).

        Returns
        -------
        Rect
            The crop rectangle.
        """
        roi_x_center = roi.x + roi.width // 2
        roi_y_center = roi.y + roi.height // 2
        crop = Rect(
            x=max(roi_x_center - (resize_width // 2), 0),
            y=max(roi_y_center - (resize_height // 2), 0),
            width=resize_width,
            height=resize_height,
        )
        return crop

    def _merge_identical_segments(
        self,
        segments: list[dict],
        video_file: VideoFile,
    ) -> list[dict]:
        """
        Merge identical segments that are next to each other.

        Parameters
        ----------
        segments: list[dict]
            speakers: list[int]
                the speaker labels of the speakers talking in the segment
            start_time: float
                the start time of the segment
            end_time: float
                the end time of the segment
            crop_type: str
                'single' for single person crop, 'split' for split screen
            x: int (for single crop)
                x-coordinate of the top left corner of the resized segment
            y: int (for single crop)
                y-coordinate of the top left corner of the resized segment
            crops: list[dict] (for split screen)
                list of crop information for each part of the split screen
        video_file: VideoFile
            The video file that the segments are from

        Returns
        -------
        list[dict]
            The merged segments.
        """
        idx = 0
        max_position_difference_ratio = 0.04
        video_width = video_file.get_width_pixels()
        video_height = video_file.get_height_pixels()

        while idx < len(segments) - 1:
            current_segment = segments[idx]
            next_segment = segments[idx + 1]

            if current_segment['crop_type'] != next_segment['crop_type']:
                idx += 1
                continue

            if current_segment['crop_type'] == 'single':
                if self._are_single_crops_similar(current_segment, next_segment, video_width, video_height, max_position_difference_ratio):
                    self._merge_single_crop_segments(current_segment, next_segment)
                    segments.pop(idx + 1)
                else:
                    idx += 1
            elif current_segment['crop_type'] == 'split':
                if self._are_split_crops_similar(current_segment['crops'], next_segment['crops'], video_width, video_height, max_position_difference_ratio):
                    self._merge_split_crop_segments(current_segment, next_segment)
                    segments.pop(idx + 1)
                else:
                    idx += 1
            else:
                # Unknown crop type, move to next segment
                idx += 1

        return segments

    def _are_single_crops_similar(self, segment1, segment2, video_width, video_height, max_diff_ratio):
        x_diff = abs(segment1['x'] - segment2['x']) / video_width
        y_diff = abs(segment1['y'] - segment2['y']) / video_height
        return x_diff < max_diff_ratio and y_diff < max_diff_ratio

    def _are_split_crops_similar(self, crops1, crops2, video_width, video_height, max_diff_ratio):
        if len(crops1) != len(crops2):
            return False
        for crop1, crop2 in zip(crops1, crops2):
            x_diff = abs(crop1['x'] - crop2['x']) / video_width
            y_diff = abs(crop1['y'] - crop2['y']) / video_height
            if x_diff >= max_diff_ratio or y_diff >= max_diff_ratio:
                return False
        return True

    def _merge_single_crop_segments(self, segment1, segment2):
        segment1['x'] = (segment1['x'] + segment2['x']) // 2
        segment1['y'] = (segment1['y'] + segment2['y']) // 2
        segment1['end_time'] = segment2['end_time']

    def _merge_split_crop_segments(self, segment1, segment2):
        for crop1, crop2 in zip(segment1['crops'], segment2['crops']):
            crop1['x'] = (crop1['x'] + crop2['x']) // 2
            crop1['y'] = (crop1['y'] + crop2['y']) // 2
        segment1['end_time'] = segment2['end_time']    

    def cleanup(self) -> None:
        """
        Remove the face detector from memory and explicity free up GPU memory.
        """
        del self._face_detector
        self._face_detector = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
