"""PyInstaller entry point for the Fathom desktop app.

Using the package implementation directly as the PyInstaller entry script
runs it as ``__main__``, which breaks its package-relative imports (``from ..``) with
"attempted relative import with no known parent package". This launcher imports the
package absolutely so those relative imports resolve.
"""

from deepseekfathom.desktop import main

if __name__ == "__main__":
    main()
