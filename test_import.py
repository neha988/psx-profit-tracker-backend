#!/usr/bin/env python
import sys
print("Python version:", sys.version)
print("Python executable:", sys.executable)

try:
    import main
    print("✓ Import successful")
    print("App:", main.app)
except Exception as e:
    import traceback
    print("✗ Import failed:")
    traceback.print_exc()
