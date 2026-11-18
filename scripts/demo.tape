# Records docs/images/demo.gif from `cotop --demo`; nothing here touches a Colab account.
# Run from the repo root: vhs scripts/demo.tape (it syncs the environment itself,
# and fails without writing the GIF if cotop doesn't start).
Output docs/images/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 1360
Set Height 820
Set Padding 12
Set Framerate 12
Set TypingSpeed 0

Hide
Type "export PS1='$ ' && uv sync -q && clear && echo synced" Enter
Wait+Screen@180s /^synced/
Type "uv run --no-sync cotop --demo" Enter
Wait+Screen@30s /compute units left/
Sleep 1s
Show
Sleep 3s
Type "j" Sleep 1.2s
Type "j" Sleep 1.2s
Type "j" Sleep 1.2s
Type "j" Sleep 2.5s
Type "j" Sleep 1.2s
Type "j" Sleep 1.2s
Type "j" Sleep 2.5s
Type "?" Sleep 3s
Type "?" Sleep 1s
Type "h" Sleep 4s
Escape Sleep 1s
Type "k" Sleep 1s
Type "s" Sleep 3s
Type "n" Sleep 2s
Hide
Type "q"
