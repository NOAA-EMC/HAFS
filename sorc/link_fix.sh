#!/bin/sh
set -x
source ./machine-setup.sh > /dev/null 2>&1

HOMEhafs=$(pwd)/..
FIXhafs=${HOMEhafs}/fix
mkdir -p ${FIXhafs}
cd ${FIXhafs}
mkdir -p fix_fv3
if [ ${target} == "wcoss_cray" ]; then
    FIXROOT=/gpfs/hps3/emc/global/noscrub/emc.glopara/git/fv3gfs/fix
elif [[ ${target} == "wcoss_dell_p3" || ${target} == "wcoss" ]]; then
    FIXROOT=/gpfs/dell2/emc/modeling/noscrub/emc.glopara/git/fv3gfs/fix
elif [ ${target} == "hera" ]; then
    FIXROOT=/scratch1/NCEPDEV/global/glopara/fix
elif [ ${target} == "theia" ]; then
    FIXROOT=/scratch4/NCEPDEV/global/save/glopara/git/fv3gfs/fix
elif [ ${target} == "jet" ]; then
    FIXROOT=/mnt/lfs1/projects/hwrf-data/git/fv3gfs/fix
else
    echo "Unknown site " ${target}
    exit 1
fi

for subdir in fix_am fix_orog fix_fv3_gmted2010 fix_sfc_climo;
do
    ln -sf ${FIXROOT}/${subdir} ./
done

echo 'done'
