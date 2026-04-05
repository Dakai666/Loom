from __future__ import annotations

import PIL.Image
from pathlib import Path
from textual.widgets import Static
from rich.text import Text

try:
    from rich_pixels import Pixels
    HAS_PIXELS = True
except ImportError:
    HAS_PIXELS = False

class ImageWidget(Static):
    """View an image natively in the terminal."""
    
    DEFAULT_CSS = """
    ImageWidget {
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        width: auto;
    }
    """

    def __init__(self, image_path: Path | str, max_terminal_rows: int = 30) -> None:
        super().__init__()
        self.image_path = Path(image_path).resolve()
        self.max_terminal_rows = max_terminal_rows

    def on_mount(self) -> None:
        if not HAS_PIXELS:
            self.update(Text(f"[Image rendering requires rich-pixels: {self.image_path.name}]", style="dim"))
            return

        if not self.image_path.exists():
            self.update(Text(f"[Image not found: {self.image_path.name}]", style="red"))
            return
            
        try:
            with PIL.Image.open(self.image_path) as img:
                w, h = img.size
                
            # rich_pixels uses 2 vertical pixels per terminal row (half blocks)
            target_pixel_h = self.max_terminal_rows * 2
            
            new_w, new_h = w, h
            if h > target_pixel_h:
                scale = target_pixel_h / h
                new_w = int(w * scale)
                new_h = target_pixel_h
                
            # Bound by an arbitrary max width (terminals are ~80-150 chars wide)
            if new_w > 100:
                scale = 100 / new_w
                new_w = 100
                new_h = int(new_h * scale)

            pixels = Pixels.from_image_path(str(self.image_path), resize=(new_w, max(1, new_h)))
            self.update(pixels)
        except Exception as e:
            self.update(Text(f"[Failed to render image: {e}]", style="red"))
