#!/bin/sh
set -x
date

ROCOTOhafs=$(pwd)
cd ${ROCOTOhafs}
EXPT=$(basename $(dirname ${ROCOTOhafs}))

# Platform
#dev="-f"
#dev="-s sites/wcoss_dell_p3.ent -f"
#dev="-s sites/wcoss_cray.ent -f"
#dev="-s sites/jet.ent -f"
dev="-s sites/hera.ent -f"

#===============================================================================
# Here are some simple examples, more examples can be seen in cronjob_hafs_rt.sh

# Run all cycles of a storm
#./run_hafs.py ${dev} 2019 05L HISTORY config.EXPT=${EXPT}# Dorian

# Run specified cycles of a storm
#./run_hafs.py ${dev} 2018083018-2018083100 06L HISTORY \
#   config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_try1 # Florence

# Run one cycle of a storm
 ./run_hafs.py -t ${dev} 2019091600 09L HISTORY config.EXPT=${EXPT}

#===============================================================================

date

echo 'cronjob done'
