"""manage the PDF transform stack during "layout" mode text extraction"""

from collections import ChainMap, Counter
from typing import TYPE_CHECKING, Any, Dict, List, MutableMapping, Union
from typing import ChainMap as ChainMapType
from typing import Counter as CounterType

from ...errors import PdfReadError
from .. import mult
from ._text_state_params import TextStateParams

if TYPE_CHECKING:
    from ._font import Font

TextStateManagerChainMapType = ChainMapType[Union[int, str], Union[float, bool]]
TextStateManagerDictType = MutableMapping[Union[int, str], Union[float, bool]]

class TextStateManager:
    """
    Tracks the current text state including cm/tm/trm transformation matrices.

    Attributes:
        transform_stack (ChainMap): ChainMap of cm/tm transformation matrices
        q_queue (Counter[int]): Counter of q operators
        q_depth (List[int]): list of q operator nesting levels
        Tc (float): character spacing
        Tw (float): word spacing
        Tz (int): horizontal scaling
        TL (float): leading
        Ts (float): text rise
        font (Font): font object
        font_size (int | float): font size
    """

    def __init__(self) -> None:
        self.transform_stack: TextStateManagerChainMapType = ChainMap(self.new_transform())
        self.q_queue: CounterType[int] = Counter()
        self.q_depth = [0]
        self.Tc: float = 0.0
        self.Tw: float = 0.0
        self.Tz: float = 100.0
        self.TL: float = 0.0
        self.Ts: float = 0.0
        self.font: Union[Font, None] = None
        self.font_size: Union[int, float, None] = None

    def set_state_param(self, op: bytes, value: Union[float, List[Any]]) -> None:
        """
        Set a text state parameter. Supports Tc, Tz, Tw, TL, and Ts operators.

        Args:
            op: operator read from PDF stream as bytes. No action is taken
                for unsupported operators (see supported operators above).
            value (float | List[Any]): new paramater value. If a list,
                value[0] is used.
        """
        if op not in [b"Tc", b"Tz", b"Tw", b"TL", b"Ts"]:
            return
        self.__setattr__(op.decode(), value[0] if isinstance(value, list) else value)

    def text_state_params(self, value: Union[bytes, str] = "") -> TextStateParams:
        """
        Create a TextStateParams instance to display a text string. Type[bytes] values
        will be decoded implicitly.

        Args:
            value (str | bytes): text to associate with the captured state.

        Raises:
            PdfReadError: if font not set (no Tf operator in incoming pdf content stream)

        Returns:
            TextStateParams: current text state parameters
        """
        if self.font is None or self.font_size is None:
            raise PdfReadError("Font and font_size not set: is PDF missing a Tf operator?")
        txt = value if isinstance(value, str) else self.decode(value)
        return TextStateParams(
            txt,
            self.font,
            self.font_size,
            self.Tc,
            self.Tw,
            self.Tz,
            self.TL,
            self.Ts,
            self.effective_transform,
        )

    def decode(self, _b: bytes) -> str:
        """
        Decode bytes from a text render operator.

        Args:
            _b: text bytes

        Returns:
            str: decoded string
        """
        if not self.font:
            raise PdfReadError("font not set: is PDF missing a Tf operator?")
        try:
            if isinstance(self.font.encoding, str):
                _text = _b.decode(self.font.encoding, "surrogatepass")
            else:
                _text = "".join(
                    self.font.encoding[x]
                    if x in self.font.encoding
                    else bytes((x,)).decode()
                    for x in _b
                )
        except (UnicodeEncodeError, UnicodeDecodeError):
            _text = _b.decode("utf-8", "replace")
        _text = "".join(
            self.font.char_map[x] if x in self.font.char_map else x for x in _text
        )
        return _text

    @staticmethod
    def raw_transform(
        _a: float = 1.0,
        _b: float = 0.0,
        _c: float = 0.0,
        _d: float = 1.0,
        _e: float = 0.0,
        _f: float = 0.0,
    ) -> Dict[int, float]:
        """Only a/b/c/d/e/f matrix params"""
        return dict(zip(range(6), map(float, (_a, _b, _c, _d, _e, _f))))

    @staticmethod
    def new_transform(
        _a: float = 1.0,
        _b: float = 0.0,
        _c: float = 0.0,
        _d: float = 1.0,
        _e: float = 0.0,
        _f: float = 0.0,
        is_text: bool = False,
        is_render: bool = False,
    ) -> TextStateManagerDictType:
        """Standard a/b/c/d/e/f matrix params + 'is_text' and 'is_render' keys"""
        result: Any = TextStateManager.raw_transform(_a, _b, _c, _d, _e, _f)
        result.update({"is_text": is_text, "is_render": is_render})
        return result

    def reset_tm(self) -> TextStateManagerChainMapType:
        """Clear all transforms from chainmap having is_text==True or is_render==True"""
        while (
            self.transform_stack.maps[0]["is_text"]
            or self.transform_stack.maps[0]["is_render"]
        ):
            self.transform_stack = self.transform_stack.parents
        return self.transform_stack

    def reset_trm(self) -> TextStateManagerChainMapType:
        """Clear all transforms from chainmap having is_render==True"""
        while self.transform_stack.maps[0]["is_render"]:
            self.transform_stack = self.transform_stack.parents
        return self.transform_stack

    def remove_q(self) -> TextStateManagerChainMapType:
        """Rewind to stack prior state after closing a 'q' with internal 'cm' ops"""
        self.transform_stack = self.reset_tm()
        self.transform_stack.maps = self.transform_stack.maps[self.q_queue.pop(self.q_depth.pop(), 0) :]
        return self.transform_stack

    def add_q(self) -> None:
        """Add another level to q_queue"""
        self.q_depth.append(len(self.q_depth))

    def add_cm(self, *args: Any) -> TextStateManagerChainMapType:
        """Concatenate an additional transform matrix"""
        self.transform_stack = self.reset_tm()
        self.q_queue.update(self.q_depth[-1:])
        self.transform_stack = self.transform_stack.new_child(self.new_transform(*args))
        return self.transform_stack

    def _complete_matrix(self, operands: List[float]) -> List[float]:
        """adds a, b, c, and d to an "e/f only" operand set a la b'Td'"""
        if len(operands) == 2:  # this is a Td operator or equivalent
            operands = [1.0, 0.0, 0.0, 1.0, *operands]
        return operands

    def add_tm(self, operands: List[float]) -> TextStateManagerChainMapType:
        """Append a text transform matrix"""
        self.transform_stack = self.transform_stack.new_child(
            self.new_transform(  # type: ignore[misc,arg-type]
                *self._complete_matrix(operands), is_text=True
            )
        )
        return self.transform_stack

    def add_trm(self, operands: List[float]) -> TextStateManagerChainMapType:
        """Append a text rendering transform matrix"""
        self.transform_stack = self.transform_stack.new_child(
            self.new_transform(  # type: ignore[misc,arg-type]
                *self._complete_matrix(operands), is_text=True, is_render=True
            )
        )
        return self.transform_stack

    @property
    def effective_transform(self) -> List[float]:
        """Current effective transform accounting for cm, tm, and trm transforms"""
        eff_transform = [*self.transform_stack.maps[0].values()]
        for transform in self.transform_stack.maps[1:]:
            eff_transform = mult(eff_transform, transform)  # type: ignore[arg-type]  # dict has int keys 0-5
        return eff_transform

    @property
    def flip_vertical(self) -> bool:
        """True if page is upside down"""
        return self.effective_transform[3] < -1e-6  # copy behavior of orient() func

    @property
    def xmaps(self) -> List[TextStateManagerDictType]:
        """Internal ChainMap 'maps' property"""
        return self.transform_stack.maps
