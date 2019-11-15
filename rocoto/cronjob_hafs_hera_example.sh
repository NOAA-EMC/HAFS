#!/bin/sh
set -x
date

ROCOTOhafs=/scratch2/BMC/wrfruc/Samuel.Trahan/crow-hafs/py3/rocoto
cd ${ROCOTOhafs}
EXPT=$(basename $(dirname ${ROCOTOhafs}))
#dev="-f"
dev="-s sites/hera.ent -f"
scrubopt="config.scrub_work=no config.scrub_com=no"

 ./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
     config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional \
     ${scrubopt}

date
echo 'cronjob done'
