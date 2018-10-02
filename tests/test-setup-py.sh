#!/bin/bash

cd $(dirname $0)
TESTS_DIR=$(pwd)
OUTDIR="$TESTS_DIR"/output/test-setup-py

# clean up any old output
mkdir -p "$OUTDIR"
rm -rf "$OUTDIR"/*

ENVDIR="$OUTDIR"/env
mkdir -p "$ENVDIR"

python -m venv "$ENVDIR" > "$OUTDIR"/virtualenv.log 2>&1
PYTHON="$ENVDIR"/bin/python3

cd ..
$PYTHON setup.py install >"$OUTDIR"/setup.log 2>&1
cd "$OUTDIR"

ok=1
for m in codar codar.cheetah codar.workflow numpy; do
    mpath=$($PYTHON -c "import $m; print($m.__file__)")
    if [ $? -ne 0 ]; then
        echo "ERROR: failed to import $m"
        ok=0
    fi
    echo $m $mpath >> "$OUTDIR"/module_paths.txt
done

if [ $ok -eq 1 ]; then
    echo "setup.py OK"
    exit 0
else
    echo "setup.py FAILED"
    exit 1
fi
