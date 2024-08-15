class Segment:
    """
    Represents a segment of a video that was cropped, including the speakers present,
    timing of the segment, and the crop coordinates.

    Attributes
    ----------
        speakers (list[int]): List of speaker IDs present in the segment.
        start_time (float): Start time of the segment in seconds.
        end_time (float): End time of the segment in seconds.
        x (int): The x coordinate of the top left corner of the segment.
        y (int): The y coordinate of the top left corner of the segment.
        width (int): The width of the crop (for split-screen segments).
        height (int): The height of the crop (for split-screen segments).
        target_x (int): The target x position in the output video (for split-screen segments).
        target_y (int): The target y position in the output video (for split-screen segments).
    """

    def __init__(
        self,
        speakers: list[int],
        start_time: float,
        end_time: float,
        x: int,
        y: int,
        width: int = None,
        height: int = None,
        target_x: int = None,
        target_y: int = None,
    ) -> None:
        """
        Initializes a Segment instance.

        Parameters
        ----------
        speakers: list[int]
            List of speaker IDs present in the segment.
        start_time: float
            Start time of the segment in seconds.
        end_time: float
            End time of the segment in seconds.
        x: int
            The x coordinate of the top left corner of the segment.
        y: int
            The y coordinate of the top left corner of the segment.
        width: int, optional
            The width of the crop (for split-screen segments).
        height: int, optional
            The height of the crop (for split-screen segments).
        target_x: int, optional
            The target x position in the output video (for split-screen segments).
        target_y: int, optional
            The target y position in the output video (for split-screen segments).
        """
        self._speakers = speakers
        self._start_time = start_time
        self._end_time = end_time
        self._x = x
        self._y = y
        self._width = width
        self._height = height
        self._target_x = target_x
        self._target_y = target_y

    @property
    def speakers(self) -> list[int]:
        """
        Returns a list of speaker identifiers in this segment. Each identifier
        uniquely represents a speaker in the video.
        """
        return self._speakers

    @property
    def start_time(self) -> float:
        """
        The start time of the segment.
        """
        return self._start_time

    @property
    def end_time(self) -> float:
        """
        The end time of the segment.
        """
        return self._end_time

    @property
    def x(self) -> int:
        """
        The x coordinate of the top left corner of the segment.
        """
        return self._x

    @property
    def y(self) -> int:
        """
        The y coordinate of the top left corner of the segment.
        """
        return self._y

    @property
    def width(self) -> int:
        """
        The width of the crop (for split-screen segments).
        """
        return self._width

    @property
    def height(self) -> int:
        """
        The height of the crop (for split-screen segments).
        """
        return self._height

    @property
    def target_x(self) -> int:
        """
        The target x position in the output video (for split-screen segments).
        """
        return self._target_x

    @property
    def target_y(self) -> int:
        """
        The target y position in the output video (for split-screen segments).
        """
        return self._target_y

    def copy(self) -> "Segment":
        """
        Returns a copy of the Segment instance.
        """
        return Segment(
            speakers=self._speakers.copy(),
            start_time=self._start_time,
            end_time=self._end_time,
            x=self._x,
            y=self._y,
            width=self._width,
            height=self._height,
            target_x=self._target_x,
            target_y=self._target_y,
        )

    def to_dict(self) -> dict:
        """
        Returns a dictionary representation of the Segment instance.
        """
        segment_dict = {
            "speakers": self._speakers,
            "start_time": self._start_time,
            "end_time": self._end_time,
            "x": self._x,
            "y": self._y,
        }
        if self._width is not None:
            segment_dict["width"] = self._width
        if self._height is not None:
            segment_dict["height"] = self._height
        if self._target_x is not None:
            segment_dict["target_x"] = self._target_x
        if self._target_y is not None:
            segment_dict["target_y"] = self._target_y
        return segment_dict

    def __str__(self) -> str:
        """
        Returns a human-readable string representation of the Segment instance,
        detailing speakers, time stamps, and crop coordinates.
        """
        base_str = (
            f"Segment(speakers: {self._speakers}, start: {self._start_time}, "
            f"end: {self._end_time}, coordinates: ({self._x}, {self._y})"
        )
        if self._width is not None and self._height is not None:
            base_str += f", size: {self._width}x{self._height}"
        if self._target_x is not None and self._target_y is not None:
            base_str += f", target: ({self._target_x}, {self._target_y})"
        base_str += ")"
        return base_str

    def __repr__(self) -> str:
        """
        Returns a string representation of the Segment instance.
        """
        return self.__str__()

    def __eq__(self, __value: object) -> bool:
        """
        Returns True if the Segment instance is equal to the given value, False
        otherwise.
        """
        if not isinstance(__value, Segment):
            return False
        return (
            self._speakers == __value.speakers
            and self._start_time == __value.start_time
            and self._end_time == __value.end_time
            and self._x == __value.x
            and self._y == __value.y
            and self._width == __value.width
            and self._height == __value.height
            and self._target_x == __value.target_x
            and self._target_y == __value.target_y
        )

    def __ne__(self, __value: object) -> bool:
        """
        Returns True if the Segment instance is not equal to the given value, False
        otherwise.
        """
        return not self.__eq__(__value)

    def __bool__(self) -> bool:
        """
        Returns True if the Segment instance is not empty, False otherwise.
        """
        return (
            bool(self._speakers)
            and bool(self._start_time)
            and bool(self._end_time)
            and bool(self._x)
            and bool(self._y)
        )