"""Render the README screenshots from the demo account; no Colab account is touched.

    uv run python scripts/screenshots.py OUT_DIR

writes OUT_DIR/dashboard.svg (138x40), small.svg (80x24), large.svg (180x52) and
stop.svg. To get the PNGs in docs/images:

    chrome --headless --force-device-scale-factor=2 --window-size=1380,880 \\
        --screenshot=docs/images/dashboard.png OUT_DIR/dashboard.svg
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from cotop.app import demo_dashboard

SIZES = {"dashboard": (138, 40), "small": (80, 24), "large": (180, 52)}


async def shoot(out: Path) -> None:
    for name, size in SIZES.items():
        with tempfile.TemporaryDirectory() as cache:
            app = demo_dashboard(Path(cache))
            async with app.run_test(size=size) as pilot:
                for _ in range(3):
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                app.save_screenshot(f"{name}.svg", path=str(out))
                if name == "dashboard":
                    await pilot.press("down", "down", "down", "down", "down", "s")
                    await pilot.pause()
                    app.save_screenshot("stop.svg", path=str(out))


if __name__ == "__main__":
    asyncio.run(shoot(Path(sys.argv[1])))
