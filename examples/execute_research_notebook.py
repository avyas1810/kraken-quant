"""Execute the research notebook from a fresh kernel, using only repo-local runtime.

Default: genuine public data. --fixture is an explicit offline verification mode.
"""
import argparse
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    runtime=root/'.notebook-runtime'
    for name in ('jupyter','ipython','matplotlib','tmp'):
        (runtime/name).mkdir(parents=True,exist_ok=True)
    os.environ['JUPYTER_RUNTIME_DIR']=str(runtime/'jupyter')
    os.environ['IPYTHONDIR']=str(runtime/'ipython')
    os.environ['MPLCONFIGDIR']=str(runtime/'matplotlib')
    os.environ['TMP']=os.environ['TEMP']=str(runtime/'tmp')
    import nbformat
    from nbclient import NotebookClient
    path=root/'notebooks/quant_kraken_research.ipynb'
    nb=nbformat.read(path,as_version=4)
    if args.fixture:
        for cell in nb.cells:
            if cell.cell_type=='code' and 'DATA_MODE = "real"' in cell.source:
                cell.source=cell.source.replace('DATA_MODE = "real"','DATA_MODE = "fixture"')
    NotebookClient(nb,timeout=900,kernel_name='python3',resources={'metadata':{'path':str(root)}}).execute()
    destination=runtime/'quant_kraken_research_fixture.ipynb' if args.fixture else path
    nbformat.write(nb,destination)
    code=[c for c in nb.cells if c.cell_type=='code']
    assert all(c.execution_count is not None for c in code)
    assert not [o for c in code for o in c.outputs if o.output_type=='error']
    print(f'Executed {len(code)} code cells successfully: {destination}')


if __name__=='__main__':
    main()
