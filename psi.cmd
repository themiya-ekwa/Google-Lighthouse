@echo off
REM Convenience wrapper so you can type `psi ...` from this folder without
REM installing the package. `pip install -e .` gives you a global `psi` command.
python -m psi %*
