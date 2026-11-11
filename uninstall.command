#!/bin/zsh
exec /usr/bin/python3 -I "${0:A:h}/setup.py" uninstall
