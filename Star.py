import os
import sys

# Fügt den 'phantom'-Ordner zum Suchpfad von Python hinzu
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/phantom"))

from run_dashboard import *

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("run_dashboard:app", host="0.0.0.0", port=8080)
