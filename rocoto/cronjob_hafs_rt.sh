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

scrubopt="config.scrub_work=no config.scrub_com=no"

#===============================================================================

# Regional static NATL basin-focused configuration with GFS nemsio format IC/BC
#./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
#    config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional_static \
#    config.NHRS=12 ${scrubopt} \
#    ../parm/hafs_regional_static.conf

# Regional storm-focused configuration with GFS nemsio format IC/BC
 ./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
     config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional \
     config.NHRS=12 ${scrubopt}

# Regional static NATL basin-focused configuration with GFS nemsio format IC and grib2ab format BC
 ./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
     config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional_static_grib2ab_lbc \
     config.ictype=gfsnemsio config.bctype=gfsgrib2ab_0p25 \
     config.NHRS=12 ${scrubopt} \
     ../parm/hafs_regional_static.conf

# Regional static NATL basin-focused configuration with GFS nemsio format IC and grib2 format BC
#./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
#    config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional_static_grib2_lbc \
#    config.ictype=gfsnemsio config.bctype=gfsgrib2_0p25 \
#    config.NHRS=12 ${scrubopt} \
#    ../parm/hafs_regional_static.conf

# Regional storm-focused configuration with GFS nemsio format IC and grib2ab format BC
#./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
#    config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional_grib2ab_lbc \
#    config.ictype=gfsnemsio config.bctype=gfsgrib2ab_0p25 \
#    config.NHRS=12 ${scrubopt}

# Regional storm-focused configuration with GFS nemsio format IC and grib2 format BC
 ./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
     config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional_grib2_lbc \
     config.ictype=gfsnemsio config.bctype=gfsgrib2_0p25 \
     config.NHRS=12 ${scrubopt}

#===============================================================================

# Global-nesting static NATL basin-focused configuration with GFS nemsio format IC/BC
 ./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
     config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_globnest_static \
     config.NHRS=12 ${scrubopt} \
     ../parm/hafs_globnest_static.conf

# Global-nesting storm-focused configuration with GFS nemsio format IC/BC
 ./run_hafs.py -t ${dev} 2019091600 09L HISTORY \
     config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_globnest \
     config.NHRS=12 ${scrubopt} \
     ../parm/hafs_globnest.conf

#===============================================================================

# Fakestorm (e.g., NATL00L) with the regional static NATL basin-focused domain configuration
#./run_hafs.py -t ${dev} 2019091600 00L HISTORY \
#    config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional_static_fakestorm \
#    config.NHRS=12 ${scrubopt} \
#    ../parm/hafs_fakestorm.conf

# Fakestorm globnest_C96s1n4_180x180 configuration
#./run_hafs.py -t ${dev} 2019091600 00L HISTORY \
#    config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_globnest_C96s1n4_180x180 \
#    config.NHRS=12 ${scrubopt} \
#    ../parm/examples/hafs_globnest_C96s1n4_180x180.conf \
#    ../parm/hafs_fakestorm.conf

# Fakestorm regional_C96s1n4_180x180 configuration
#./run_hafs.py -t ${dev} 2019091600 00L HISTORY \
#    config.EXPT=${EXPT} config.SUBEXPT=${EXPT}_rt_regional_C96s1n4_180x180 \
#    config.NHRS=12 ${scrubopt} \
#    ../parm/examples/hafs_regional_C96s1n4_180x180.conf \
#    ../parm/hafs_fakestorm.conf

#===============================================================================

date

echo 'cronjob done'
