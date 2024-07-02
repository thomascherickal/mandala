### convert .ipynb files in this directory to .md files

import os
import argparse

# parse the command line arguments
parser = argparse.ArgumentParser(description='Convert .ipynb files to .md files')
parser.add_argument('--filenames', type=str, nargs='+', help='list of filenames to convert')
args = parser.parse_args()


if __name__ == '__main__':
    # find the names of the .ipynb files in this directory
    if args.filenames:
        ipynb_files = args.filenames
    else:
        ipynb_files = [f for f in os.listdir() if f.endswith('.ipynb')]
    # convert each one to .md
    for f in ipynb_files:
        os.system('jupyter nbconvert --to notebook --execute --inplace ' + f)
        os.system(f"jupyter nbconvert --to markdown {f}")
    
    DOCS_REL_PATH = '../../docs/docs/'

    # now, move the .md files to the docs directory
    for f in ipynb_files:
        os.system("mv " + f.replace('.ipynb', '.md') + " " + DOCS_REL_PATH)
    
    # also, move any directories named "{fname}_files" to the docs directory
    for f in ipynb_files:
        files_folder = f.replace('.ipynb', '_files')
        if os.path.isdir(files_folder):
            # first, remove the directory if it already exists
            os.system(f"rm -r {DOCS_REL_PATH}" + files_folder)
            # then, move the directory
            os.system("mv " + f.replace('.ipynb', '_files') + " " + DOCS_REL_PATH)
