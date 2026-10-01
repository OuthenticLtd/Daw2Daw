# DAW Converter (website)

Converts Cubase projects to REAPER and REAPER projects to Cubase, in the
browser: https://outhenticltd.github.io/daw-converter/

The page runs the converter locally with Pyodide (Python in WebAssembly)
and ffmpeg.wasm; projects never leave the visitor's computer. This
repository only holds the built site; it is generated from the converter's
own repository (`python web/build_web.py`).
