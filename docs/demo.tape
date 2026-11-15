# docs/demo.tape: the GIF of `piv demo`. Record it with: vhs docs/demo.tape
# Not recorded yet: an automated recording is refused until `piv demo` has an instant no-Kaggle mode; a plain
# `piv demo` makes real model and Kaggle calls, so this tape is not run here. Record it once that mode is built.
Output docs/demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1100
Set Height 620
Set TypingSpeed 40ms

Type "piv demo"
Enter
Wait+Screen@3m /run links:/
Sleep 3s

Type "piv demo --clean"
Enter
Wait+Screen@1m /deleted/
Sleep 2s
