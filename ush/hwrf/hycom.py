
"""This module will one day contain the implementation of the HyCOM
initialization and forecast jobs."""

import re, sys, os, glob, datetime, math, fractions, collections, subprocess
import produtil.fileop, produtil.log
import hwrf.numerics, hwrf.input
import hwrf.hwrftask, hwrf.coupling, hwrf.exceptions
import time, shutil
from produtil.cd import NamedDir
from produtil.fileop import make_symlink, isnonempty, remove_file, \
    deliver_file, gribver, wait_for_files
from produtil.datastore import FileProduct, COMPLETED, RUNNING, FAILED
from produtil.run import *
from produtil.log import jlogger
from hwrf.numerics import to_datetime,to_datetime_rel,TimeArray,to_fraction,\
    to_timedelta

NO_DEFAULT=object()

def yesno(x):
    return 'YES' if x else 'NO'

hycom_epoch=datetime.datetime(1900,12,31,0,0,0)

def date_hycom2normal(hycom):
    if isinstance(hycom,basestring):
        hycom=float(hycom)
    if isinstance(hycom,int):
        hycom=datetime.timedelta(hours=hycom*24)
    elif isinstance(hycom,float):
        hycom=datetime.timedelta(hours=hycom*24)
    elif isinstance(hycom,fractions.Fraction):
        hycom=datetime.timedelta(hours=float(hycom*24))
    return hycom_epoch+hycom

def date_normal2hycom(normal):
    if not isinstance(normal,datetime.datetime):
        normal=to_datetime(normal)
    return to_fraction(normal-hycom_epoch)/(3600*24)

def scriptexe(task,path):
    """Generates a produtil.prog.Runner for running a Hycom ksh
    script.  Adds a bunch of variables from config sections."""
    there=task.confstrinterp(path)
    e=exe(there)
    vars=dict()
    for k,v in task.conf.items(task.confstr('strings','hycomstrings')):
        vars[k]=str(v)
    for k,v in task.conf.items(task.confstr('bools','hycombools')):
        vars[k]=yesno(v)
    RTOFSDIR=task.meta('RTOFSDIR','')
    if RTOFSDIR:
        vars['RTOFSDIR']=RTOFSDIR
    return    e.env(**vars)

def read_RUNmodIDout(path):
    RUNmodIDout=''
    with open(path,'rt') as f:
        for line in f:
            m=re.match('^export RUNmodIDout=(.*)$',line)
            if m:
                RUNmodIDout=m.groups()[0]
    return RUNmodIDout

class HYCOMInit1(hwrf.hwrftask.HWRFTask):
    def remove_ocean(): self.uncouple()

    def __init__(self,dstore,conf,section,taskname=None,fcstlen=126,
                 **kwargs):
        super(HYCOMInit1,self).__init__(dstore,conf,section,
                                       taskname=taskname,**kwargs)
        self.forecast_exe=None
        self.run_coupled=True
        self.fcstlen=fcstlen
        self.make_products()
        self.spinlength=self.confint('spinlength',0)
        self.ic=None
        self.jc=None
        self.idm=None
        self.jdm=None
        self.ijgrid=None
        self.Application=None
        self.__rtofs_inputs=None
        self.__rtofs_inputs_ymd=None
        self.__blkdat=None

    def make_products(self):
        """Initializes all Product objects to make them available to
        future jobs."""
        # Add the HyCOM-specific products whose delivery location is
        # in COM with the standard output file prefix
        # (invest99l.2017110318).
        logger=self.log()
        self.hycom_settings=FileProduct(
            self.dstore,'hycom_settings',self.taskname,location=
            self.confstrinterp('{com}/{out_prefix}.hycom_settings'))

        # prodnameA and prodnameB are three-hourly:
        fhrs=range(int(self.fcstlen+25.001))
        fhrs=fhrs[0::3]
        atime=to_datetime(self.conf.cycle)
        ftimes=[to_datetime_rel(t*3600,atime) for t in fhrs]
        self.init_file2a=TimeArray(atime,ftimes[-1],3*3600.0)
        self.init_file2b=TimeArray(atime,ftimes[-1],3*3600.0)
        for ftime in ftimes:
            prodnameA=self.timestr('hmon_basin.{fahr:03d}.a',ftime,atime)
            filepathA=self.timestr('{com}/{out_prefix}.{pn}',pn=prodnameA)
            prodnameB=self.timestr('hmon_basin.{fahr:03d}.b',ftime,atime)
            filepathB=self.timestr('{com}/{out_prefix}.{pn}',pn=prodnameB)
            self.init_file2a[ftime]=FileProduct(
                self.dstore,prodnameA,self.taskname,location=filepathA)
            self.init_file2b[ftime]=FileProduct(
                self.dstore,prodnameB,self.taskname,location=filepathB)

        # initial conditions:
        self.restart_out=dict()
        what='restart_out'
        for ab in 'ab':
                local=what+'.'+ab # like restart_out.a or restart_outR.b
                self.restart_out[local]=FileProduct(self.dstore,local,self.taskname)

        self.spin_archv_a=FileProduct(self.dstore,'spin_archv_a',self.taskname)
        self.spin_archv_b=FileProduct(self.dstore,'spin_archv_b',self.taskname)

        self.blkdat_input=FileProduct(
            self.dstore,'blkdat.input',self.taskname,location=
            self.confstrinterp('{com}/{out_prefix}.standalone.blkdat.input'))

    def last_lead_time_today(self,cychour):
        if cychour<6: return 0
                 # 96 hours available minus 6z cycle
#        if cychour<12: return 96   DAN - 6z and 12z RTOFS runs too late so get from PDYm1
        if cychour<18: return 0
        return 192

    def fill_ocstatus(self,ocstatus,logger):
        """Fills the ocean status files with information.  This is
        called from exhwrf_ocean_init after a successful call to the
        run() function below.  The ocstatus argument is the
        hwrf.coupling.OceanStatus object that fills the files."""
        # First argument: True=run coupled, False=don't
        # Second argument: the logger argument sent to this function
        # Third argument: a list of extra lines to dump into fill_ocstatus
        ocstatus.set(self.run_coupled,logger,['forecast_exe=%s'%self.forecast_exe])

    def recall_ocstatus(self,ocstatus,logger):
        """Reads the ocean status back in during the forecast and
        check_init jobs, filling the self.run_coupled,
        self.forecast_exe variables."""
        # Get the name of the first ocstatus file for error reporting:
        for filename in ocstatus.fileiter():
            break

        # Read the lins of the ocstatus file:
        lines=ocstatus.read(logger)
        for line in lines:
            # See if any lines are KEY=VALUE lines, and split them into parts
            m=re.match('^ *([^ =]+) *= *(.*?) *$',line)
            if not m:
                logger.warning('%s: unparseable ocstatus line: %s'
                               %(filename,line))
                continue
            (key,value)=m.groups()
            value=value.strip()
            key=key.strip()

            # See if any recognized key=value lines are present:
            if key=='RUN_COUPLED':
                if value=='YES':     self.run_coupled=True
                elif value=='NO':    self.run_coupled=False
                else:
                    logger.warning('%s: ignoring unrecognized RUN_COUPLED value %s'%(
                            filename,repr(value)))
            elif key=='forecast_exe': self.forecast_exe=value
            else:
                logger.warning('%s: ignoring unknown key %s'%(filename,key))

    def find_rtofs_data(self):
        """!Fills the RTOFS staging area with RTOFS data."""

        logger=self.log()
        cyc=self.conf.cycle

        rtofs_atime=datetime.datetime(cyc.year,cyc.month,cyc.day,0)
        rtofs_ymd=rtofs_atime.strftime('%Y%m%d')

        # Input directories:
        inputs=self.rtofs_inputs
        logger.info('FRD: inputs=%s'%(repr(inputs)))
        oceands=self.confstr('ocean_dataset')
        ocean_now=self.confstr('ocean_now')
        ocean_fcst=self.confstr('ocean_fcst')

        logger.info('FRD: oceands=%s'%(repr(oceands)))

        cyc=self.conf.cycle
        oceanatime=datetime.datetime(cyc.year,cyc.month,cyc.day,0,0,0)
        cyctime=to_timedelta(cyc.hour*3600)
        logger.info('FRD: oceanatime=%s'%(repr(oceanatime)))
        logger.info('FRD: cyctime=%s'%(repr(cyctime)))

# find the latest RTOFS data for IC:
        if cyc.hour==0:
           zeroloc=inputs.locate(oceands,ocean_now,atime=oceanatime,ab='a')
        else:
           zeroloc=inputs.locate(oceands,ocean_fcst,atime=oceanatime,ftime=oceanatime+cyctime,ab='a')

        zerodir=os.path.dirname(zeroloc)
        zerostat=-1
        if isnonempty(zeroloc):
            zerostat=0

        logger.info('FRD1: zeroloc=%s zerostat=%d '%(repr(zeroloc),zerostat))

        loc0=inputs.locate(oceands,ocean_now,atime=oceanatime,ab='a')
        dir0=os.path.dirname(loc0)

        # Decide the staging directory:
        outdir=self.confstr('RTOFS_STAGE','')
        if not outdir:
            outdir=os.path.join(self.workdir,rtofs_atime.strftime('rtofs.%Y%m%d'))

        # Get data:
        ni=hwrf.namelist.NamelistInserter(self.conf,self.section)
        with NamedDir(outdir,keep=True,logger=logger,rm_first=False) as d:
            parmin=self.confstrinterp('{PARMhycom}/hmon_get_rtofs.nml.in')
            parmout='get_rtofs.nml'

# from PDY (loc0)
            if zerostat==0:
                lastleadtimetoday=0
                #starthr=cyc.hour-24
                # need to change it into
                starthr=cyc.hour
                endhr=cyc.hour
                logger.info('FRDa: dir0=%s starthr=%d endhr=%d lastleadtimetoday=%d'%(repr(dir0),starthr,endhr,lastleadtimetoday))
                with open(parmin,'rt') as inf:
                    with open(parmout,'wt') as outf:
                        outf.write(ni.parse(inf,logger,parmin,atime=rtofs_atime,
                            INDIR1=dir0,INDIR2=dir0,INDIR3=dir0,
                            STARTHR=starthr,ENDHR=endhr,
                            LAST_LEAD_TIME_TODAY=96))
                checkrun(mpirun(mpi(self.getexe('hafs_get_rtofs')),allranks=True),logger=logger)
                os.rename('get_rtofs.nml','get_rtofs.nml.0')

    def run(self):
        """Runs the hycom initialization for hycominit1.  Raises an exception if
        something goes wrong.  Returns on success."""
        logger=self.log()
        # Reset coupling status information:
        self.forecast_exe=None
        self.run_coupled=False
        try:
            self.state=RUNNING
            # Inside the "with" block, we create and cd to the work
            # directory and then cd back out at the end.  The
            # "rm_first=True" means the directory is deleted first if
            # it already exists upon entry of the "with" block.
	    print "self.workdir", self.workdir
            with NamedDir(self.workdir,keep=not self.scrub,
                          logger=logger,rm_first=True) as d:
                self.select_domain(logger)
                self.hycom_settings.deliver(frominfo='./hycom_settings')

                self.find_rtofs_data()

                # Create BC and IC for this domain 
                self.create_bc_ic(logger)
                #         logger=logger)

                # Find the runmodidout with respect to domain
                RUNmodIDout=self.RUNmodIDout
                RUNmodIDout=read_RUNmodIDout('./hycom_settings')

                # Deliver restart files
                for(prodname,prod) in self.restart_out.iteritems():
                    (local,ab)=prodname.split('.')
                    loc=self.timestr('{'+local+'}',ab=ab,RUNmodIDout=RUNmodIDout)
                    prod.deliver(location=loc,frominfo=prodname,
                                 keep=True,logger=logger)

                # Make the flag file to indicate we're done.
                done=self.timestr('{com}/{vit[stnum]:02d}{vit[basin1lc]}.hycom1_init.done')
                with open(done,'wt') as f:
                    f.write('hycom1 init done for this cycle\n')

                # Make sure we run coupled:
                self.run_coupled=True
            self.state=COMPLETED
        except Exception as e:
            logger.error('Unhandled exception in ocean init: %s'
                         %(str(e),),exc_info=True)
            self.state=FAILED
            raise
        except:  # fatal signal, other catastrophic errors
            self.state=FAILED
            raise

    def inputiter(self):
        """!Iterates over all needed input data."""
        atmos1ds=self.confstr('atmos1_dataset')
        atmos2ds=self.confstr('atmos2_dataset')
        atmos1_flux=self.confstr('atmos1_flux')
        atmos1_grid=self.confstr('atmos1_grid')
        atmos2_flux=self.confstr('atmos2_flux')
        atmos2_grid=self.confstr('atmos2_grid')
        oceands=self.confstr('ocean_dataset')
        ocean_past=self.confstr('ocean_past')
        ocean_now=self.confstr('ocean_now')
        ocean_rst=self.confstr('ocean_rst')
        ocean_fcst=self.confstr('ocean_fcst')
        spinlength=self.confint('spinlength',0)
        logger=self.log()
        hwrfatime=self.conf.cycle
        hwrf_start=0
        hwrf_finish=int(math.ceil(float(self.fcstlen)/3)*3)
        fcstint=self.confint('forecast_forcing_interval',3)
        # Assume 24 hour cycle for ocean model.

        # oceanatime is today's RTOFS analysis time as a datetime:
        todayatime=datetime.datetime(
            hwrfatime.year,hwrfatime.month,hwrfatime.day)

        # prioratime is yesterday's RTOFS analysis time as a datetime:
        prioratime=to_datetime_rel(-24*3600,todayatime)

        # Last allowed lead time today:
        last_fhr_today=self.last_lead_time_today(hwrfatime.hour)

        # Epsilon for time comparisons:
        epsilon=to_fraction(900) # 15 minutes

        # Loop over all forecast hours from -24 to the end of the HWRF
        # forecast, relative to the HWRF analysis time.  We round up
        # to the next nearest 3 hours since the RTOFS outputs
        # three-hourly:

        logger.info("In hycom inputiter, todayatime=%s prioratime=%s "
                    "epsilon=%s hwrf_start=%s hwrf_finish=%s "
                    "last_fhr_today=%s"%(
                repr(todayatime),repr(prioratime),repr(epsilon),
                repr(hwrf_start),repr(hwrf_finish),repr(last_fhr_today)))

        yield dict(dataset=oceands,item=ocean_rst,
                   atime=prioratime,ftime=prioratime,ab='a')
        yield dict(dataset=oceands,item=ocean_rst,
                   atime=prioratime,ftime=prioratime,ab='b')

        for fhr in xrange(hwrf_start,hwrf_finish+3,6):

            # The ftime is the desired forecast time as a datetime:
            ftime=to_datetime_rel(fhr*3600,hwrfatime)

            # Which RTOFS cycle do we use?
            if fhr>last_fhr_today:
                oceanatime=prioratime
            else:
                oceanatime=todayatime

            # The oceanftime is the forecast lead time for TODAY's
            # RTOFS as a datetime.timedelta.
            oceanftime=ftime-oceanatime

            # The oceanfhr is the RTOFS forecast hour as an integer
            # for TODAY's RTOFS forecast.
            oceanfhr=int(round(to_fraction(oceanftime,negok=True)))

            logger.info('fhr=%s ftime=%s oceanatime=%s oceanftime=%s oceanfhr: to_fraction=%s round=%s int=%s'%(
                    repr(fhr),repr(ftime),repr(oceanatime),repr(oceanftime),
                    repr(to_fraction(oceanftime,negok=True)),
                    repr(round(to_fraction(oceanftime,negok=True))),
                    repr(oceanfhr)))

            # Nowcasts are always from current RTOFS cycle since
            # they're available before HWRF 0Z starts.
            if oceanfhr<0:
                logger.info('For fhr=%s oceanfhr=%s use ocean_past atime=%s ftime=%s'%(
                        repr(fhr),repr(oceanfhr),repr(oceanatime),repr(ftime)))
                yield dict(dataset=oceands,item=ocean_past,
                           atime=oceanatime,ftime=ftime,ab='a')
                yield dict(dataset=oceands,item=ocean_past,
                           atime=oceanatime,ftime=ftime,ab='b')
                continue
            elif oceanfhr==0:
                logger.info('For fhr=%s oceanfhr=%s use ocean_now atime=%s ftime=%s'%(
                        repr(fhr),repr(oceanfhr),repr(oceanatime),repr(ftime)))
                yield dict(dataset=oceands,item=ocean_now,
                           atime=oceanatime,ftime=ftime,ab='a')
                yield dict(dataset=oceands,item=ocean_now,
                           atime=oceanatime,ftime=ftime,ab='b')
                yield dict(dataset=oceands,item=ocean_rst,
                           atime=oceanatime,ftime=ftime,ab='a')
                yield dict(dataset=oceands,item=ocean_rst,
                           atime=oceanatime,ftime=ftime,ab='b')
                continue

            # Later times' availability depends on HWRF forecast
            # cycle:
            #   HWRF 00Z => RTOFS today available through 00Z
            #   HWRF 06Z => RTOFS today available through +96 hours
            #   HWRF 12Z => RTOFS today available through +192 hours
            #   HWRF 18Z => RTOFS today available through +192 hours

            chosen_atime=oceanatime
            if oceanfhr<=96 and hwrfatime.hour<6:
                chosen_atime=prioratime
            if oceanfhr<96 and hwrfatime.hour<12:
                chosen_atime=prioratime

            logger.info('For fhr=%s oceanfhr=%s use ocean_fcst atime=%s ftime=%s'%(
                    repr(fhr),repr(oceanfhr),repr(chosen_atime),repr(ftime)))
            yield dict(dataset=oceands,item=ocean_fcst,
                       atime=chosen_atime,ftime=ftime,ab='a')
            yield dict(dataset=oceands,item=ocean_fcst,
                       atime=chosen_atime,ftime=ftime,ab='b')

    def create_bc_ic(self,logger):
        thiscycle=self.conf.cycle
        prevcycle=to_datetime_rel(-6*3600,self.conf.cycle)
        spinlen=self.spinlength
        fsecs=self.fcstlen*3600
        fcstlen=(fsecs+1800)//3600
        end=to_datetime_rel(fcstlen*3600,self.conf.cycle)

        ocean_status=0
        boundary_conditions_from_rtofs=True
        same_domain=1
        force_coldstart=0

        if same_domain==1 and force_coldstart==0:
            spin=True
            create_subdomain=False

        logger.info('Create subdomain from RTOFS.')
        self.rtofs_subset_bdry_init(logger)

        logger.info('Spin up analysis.')
        self.rtofs_spin(logger)

    def select_domain(self,logger):
        atmos_lon=self.conffloat('domlon','cenlo',section='config')
        # Fit lon within [-180,180)
        atmos_lon=(3600+int(round(atmos_lon))+180)%360-180
        basin=self.storminfo.pubbasin2
        nhbasin = basin in ('AL', 'EP', 'CP', 'WP', 'IO')
        Application=None
        if basin=='AL':
            Application='hat10_basin'
        elif basin=='CP':
            Application='hcp70_basin'
        elif nhbasin and atmos_lon>-180 and atmos_lon<=20:
            Application='hep20_basin'
        elif nhbasin and atmos_lon>100:
            Application='hwp30_basin'
        elif nhbasin and atmos_lon>20 and atmos_lon<=100:
            Application='hin40_basin'
        elif basin in [ 'SL', 'LS' ]:
            Application='hsn50_basin'
        elif basin in [ 'SH', 'SP', 'SI' ]:
            Application='hsp60_basin'
        else:
            msg='No ocean basin available for basin=%s lat=%s.  Run uncoupled.'%(
                basin,repr(atmos_lon))
            jlogger.warning(msg)
            raise hwrf.exceptions.NoOceanBasin(msg)
        self.RUNmodIDin='rtofs_glo'
        RUNmodIDin=self.RUNmodIDin
        
        aptable=self.confstrinterp('{PARMhycom}/hmon_rtofs.application_table')
        found=False
        with open(aptable,'rt') as apfile:
            for line in apfile:
                fields=line.split()
                if len(fields)<9:
                    logger.info('%s: ignore line %s'%(aptable,line.strip()))
                    continue
                (ap,inmod,outmod,gridid,proc,np1,np2,mp1,mp2) = fields
                if ap==Application and inmod==self.RUNmodIDin:
                    found=True
                    self.RUNmodIDout=outmod
                    self.gridid=gridid
                    found=True
        del line,apfile
        if not found:
            msg='%s: could not find Application=%s RUNmodIDin=%s'%(
                aptable,repr(Application),repr(self.RUNmodIDin))
            logger.error(msg)
            raise hwrf.exceptions.InvalidOceanInitMethod(msg)

        gridtable=self.confstrinterp('{PARMhycom}/hmon_rtofs.grid_table')
        found=False
        with open(gridtable,'rt') as gridfile:
            for line in gridfile:
                fields=line.split()
                if len(fields)<11:
                    logger.info('%s: ignore line %s'%(gridtable,line.strip()))
                    continue
                (ingridid,gridlabelin,gridlabelout,grid_source,\
                     idm,jdm,kdm,ic,jc,ijgrid,gridno) = fields
                idm=int(idm)
                jdm=int(jdm)
                kdm=int(kdm)
                ic=int(ic)
                jc=int(jc)
                ijgrid=int(ijgrid)
                gridno=int(gridno)
                if ingridid==self.gridid and line.find(RUNmodIDin)>=0:
                    found=True
                    ( self.idm, self.jdm, self.kdm, self.kkdm ) = \
                     (     idm,      jdm,      kdm,      kdm  )
                    ( self.ic, self.jc, self.ijgrid, self.gridno ) = \
                     (     ic,      jc,      ijgrid,      gridno )
                    ( self.gridlabelin, self.gridlabelout ) = \
                     (     gridlabelin,      gridlabelout )
        if not found:
            msg='%s: could not find grid=%s RUNmodIDin=%s'%(
                gridtable,repr(self.gridid),repr(RUNmodIDin))
            logger.error(msg)
            raise hwrf.exceptions.InvalidOceanInitMethod(msg)
        assert(self.section=='hycominit1')
        self.conf.set(self.section,'RUNmodIDin',self.RUNmodIDin)
        self.conf.set(self.section,'gridlabelin',self.gridlabelin)
        self.conf.set(self.section,'gridlabelout',self.gridlabelout)
        self.conf.set(self.section,'RUNmodIDout',self.RUNmodIDout)
        with open('hycom_settings','wt') as f:
            f.write('''export idm={idm}
export jdm={jdm}
export kdm={kdm}
export kkdm={kkdm}
export ic={ic}
export jc={jc}
export ijgrid={ijgrid}
export gridlabelout={gridlabelout}
export gridlabelin={gridlabelin}
export RUNmodIDout={RUNmodIDout}
export RUNmodIDin={RUNmodIDin}
export gridno={gridno}\n'''.format(**self.__dict__))

        logger.info('HYCOM grid: i,j,kdm=%d,%d,%d ic,jc=%d,%d ijgrid=%d gridno=%d'%(
                idm,jdm,kdm,ic,jc,ijgrid,gridno))
        jlogger.info('HYCOM domain: in=%s.%s out=%s.%s'%(
                self.RUNmodIDin,self.gridlabelin,
                self.RUNmodIDout,self.gridlabelout))

    @property
    def rtofs_inputs(self):
        if self.__rtofs_inputs is None:
            hd=self.confstr('catalog','hwrfdata')
            inputs=hwrf.input.DataCatalog(self.conf,hd,self.conf.cycle)
            self.__rtofs_inputs=inputs
        return self.__rtofs_inputs

    @property
    def rtofs_inputs_ymd(self):
        if self.__rtofs_inputs_ymd is None:
            hd=self.confstr('catalog','hwrfdata')
            inputs=hwrf.input.DataCatalog(self.conf,hd,self.conf.cycle)
            self.__rtofs_inputs_ymd=inputs
        return self.__rtofs_inputs_ymd

    def rtofs_subset_bdry_init(self,logger):
        inputs=self.rtofs_inputs_ymd
        logger.info('RI: inputsymd=%s'%(repr(inputs)))
        def linkf(ffrom,fto):
            produtil.fileop.make_symlink(ffrom,fto,force=True,logger=logger)

        RUNmodIDin=self.RUNmodIDin
        RUNmodIDout=self.RUNmodIDout
        gridlabelin=self.gridlabelin
        gridlabelout=self.gridlabelout

        if int(self.ijgrid)==1:
            cmd=alias(batchexe(self.getexe('hafs_rtofs_subregion')))
        elif int(self.ijgrid)==2:
            cmd=alias(batchexe(self.getexe('hafs_isubregion2avg')))
        else:
            msg='Invalid ijgrid value %s'%(repr(ijgrid),)
            logger.critical(msg)
            raise hwrf.exceptions.InvalidOceanInitMethod(msg)

        self.linkab('{FIXhycom}/%s.%s.regional.grid'%(RUNmodIDin,gridlabelin),
             'regional.grid')
        self.linkab('{FIXhycom}/%s.%s.regional.depth'%(RUNmodIDin,gridlabelin),
             'regional.depth')
        self.linkab('{FIXhycom}/hmon_%s.%s.regional.grid'%(RUNmodIDout,gridlabelout),
             'regional.subgrid')
        self.linkab('{FIXhycom}/hmon_%s.%s.regional.depth'%(RUNmodIDout,gridlabelout),
             'regional.subdepth')

        cyc=self.conf.cycle

        # --- FIND INPUTS FROM PAST DAYS AND LINK TO archs_in.%d.ab (odd counts in hrrange)
        # --- hsk: and archv_in.%d.ab (even counts)
        outdir=self.confstr('RTOFS_STAGE','')
        icount=-1
        atype='v'

        ihr=0
        icount+=1
        now=to_datetime_rel(ihr*3600.0,cyc)
        filestringtime=now.strftime('%Y%m%d_%H%M%S')
        archva='%s/rtofs_%s_arch%s.a'%(outdir,filestringtime,atype)
        archvb='%s/rtofs_%s_arch%s.b'%(outdir,filestringtime,atype)
        linkf(archva,'archv_in.%d.a'%icount)
        linkf(archvb,'archv_in.%d.b'%icount)
        subregion_in='subregion.%d.in'%icount
        subregion_out='subregion.%d.out'%icount
        with open(subregion_in,'wt') as f:
            f.write("""archv_in.%d.b
hmon_basin.%03d.b
subregion %s
%d         'idm   ' = longitudinal array size
%d         'jdm   ' = latitudinal  array size
%d         'irefi ' = 1st index origin of subgrid or 0 if NOT aligned
%d         'jrefi ' = 2nd index origin of subgrid
1          'irefo ' = longitude output reference location
1          'jrefo ' = latitude output reference location
0          'iceflg' = ice in output archive flag (0=none,1=energy loan model)
""" %(
                icount, icount*3, self.Application,
                int(self.idm),int(self.jdm),
                int(self.ic),int(self.jc)))

        # --- RUN SUBREGIONING PROGRAM
        checkrun(cmd<subregion_in,logger=logger)

    def rtofs_spin(self,logger):
        cyc=self.conf.cycle
        spinstart=to_datetime_rel(-self.spinlength*3600,cyc)

        epsilon=30
        outdir=self.confstr('RTOFS_STAGE','')
        now=datetime.datetime(cyc.year,cyc.month,cyc.day,0,0,0)
        filestringtime=now.strftime('%Y%m%d_%H%M%S')
        restart_in_a='%s/rtofs_%s_restart.a'%(outdir,filestringtime)
        restart_in_b='%s/rtofs_%s_restart.b'%(outdir,filestringtime)
        logger.info('now=%s filestringtime=%s restart_in_a=%s'%(repr(now),repr(filestringtime),repr(restart_in_a)))

        if restart_in_a is None:
            msg='No rtofs restart file found.  Giving up.'
            jlogger.error(msg)
            if not allow_fallbacks and not expect:
                raise hwrf.exceptions.OceanRestartMissing(msg)

        self.restart2restart(restart_in_a,restart_in_b,
                             'restart_pre.a','restart_pre.b',logger)
        with open('restart_pre.b','rt') as bf:
            bf.readline()
            splat=bf.readline().split()
            rydf=float(splat[4])
            ryd=date_hycom2normal(rydf)
            
        logger.info('SOME STRING FOR WHICH TO GREP - ryd=%s spinstart=%s ryd-spinstart=%s to_fraction(...)=%s abs=%s epsilon=%s'%(
                    repr(ryd),repr(spinstart),repr(ryd-spinstart),repr(to_fraction(ryd-spinstart,negok=True)),
                    repr(abs(to_fraction(ryd-spinstart,negok=True))),repr(epsilon)))
        if abs(to_fraction(ryd-spinstart,negok=True))<epsilon:
            logger.info('Restart is at right time.  Move restart_pre to restart_forspin.')
            self.moveab('restart_pre','restart_out')
        else:
            logger.info('Restart is at wrong time (%s instead of %s).  Will use archv2restart.'%(
                    ryd.strftime("%Y%m%d%H"),spinstart.strftime('%Y%m%d%H')))
            self.archv2restart(logger)

    def blkflag(self,flagname,default=NO_DEFAULT):
        blkdat=self.blkdat
        if  default is not NO_DEFAULT and \
                flagname not in blkdat:
            return default
        return abs(self.blkdat[flagname][0])>.01

    def copyab(self,sfrom,fto):
        ffrom=self.timestr(sfrom)
        produtil.fileop.deliver_file(
            ffrom+'.a',fto+'.a',keep=True,logger=self.log())
        produtil.fileop.deliver_file(
            ffrom+'.b',fto+'.b',keep=True,logger=self.log())

    def moveab(self,ffrom,fto):
        deliver_file(ffrom+'.a',fto+'.a',keep=False,logger=self.log())
        deliver_file(ffrom+'.b',fto+'.b',keep=False,logger=self.log())

    def linkab(self,sfrom,fto):
        ffrom=self.timestr(sfrom)
        produtil.fileop.make_symlink(
            ffrom+'.a',fto+'.a',force=True,logger=self.log())
        produtil.fileop.make_symlink(
            ffrom+'.b',fto+'.b',force=True,logger=self.log())

    @property
    def blkdat(self):
        if self.__blkdat is not None: return self.__blkdat
        d=collections.defaultdict(list)
        self.__blkdat=d
        blkdat_input=self.timestr('{PARMhycom}/hmon_{RUNmodIDout}.{gridlabelout}.fcst.blkdat.input')
        with open(blkdat_input) as f:
            for line in f:
                m=re.match("^\s*(\S+)\s*'\s*(\S+)\s*' = ",line)
                if m:
                    (val,kwd)=m.groups(0)
                    val=float(val)
                    d[kwd].append(val)
        return self.__blkdat

    @property
    def baclin(self):
        return self.blkdat['baclin'][0]

    def archv2restart(self,logger):
        self.linkab('{FIXhycom}/hmon_{RUNmodIDout}.{gridlabelout}.regional.grid',
             'regional.grid')
        self.linkab('{FIXhycom}/hmon_{RUNmodIDout}.{gridlabelout}.regional.depth',
             'regional.depth')
        self.linkab('{FIXhycom}/hmon_{RUNmodIDout}.{gridlabelout}.regional.grid',
             'regional.grid')
        self.linkab('{FIXhycom}/hmon_{RUNmodIDout}.{gridlabelout}.regional.depth',
             'regional.depth')

        baclin=self.baclin

        self.linkab('hmon_basin.000','archv2r_in')
        if isnonempty('restart_forspin.b'):
            self.linkab('restart_forspin','restart_in')
        elif isnonempty('restart_pre.b'):
            self.linkab('restart_pre','restart_in')
        remove_file('archv2restart.in',info=True,logger=logger)
        remove_file('archv2restart.out',info=True,logger=logger)
        remove_file('restart_out.b',info=True,logger=logger)
        remove_file('restart_out.a',info=True,logger=logger)
        with open('archv2restart.in','wt') as arin:
            arin.write('''archv2r_in.b
restart_in.a
restart_out.a
20                 'iexpt '   = experiment number x10  (000=from archive file)
3                  'yrflag'   = days in year flag (0=360J16,1=366J16,2=366J01,3=actual)
%d                 'idm   '   = longitudinal array size
%d                 'jdm   '   = latitudinal  array size
2         'kapref'   = thermobaric reference state (-1 to 3, optional, default 0)
%d                 'kdm   '   = number of layers
34.0               'thbase' = reference density (sigma units)
%f                 'baclin' = baroclinic time step (seconds)
'''%( self.idm, self.jdm, self.kdm, baclin))

#        cmd=( mpirun(exe(self.getexe('hmon_archv2restart'))) \
#                  < 'archv2restart.in' )\
#                  > 'archv2restart.out'

#	cmd=self.getexe('hmon_archv2restart')
#        checkrun(exe(cmd<'archv2restart.in',logger=logger))

        cmd=( exe(self.getexe('hafs_archv2restart')) < 'archv2restart.in' )
        checkrun(cmd,logger=logger)


    def restart2restart(self,in_a,in_b,out_a,out_b,logger):
        cyc=self.conf.cycle
        with open('regional.grid.b','rt') as rgbf:
            idm_more=rgbf.readline().split()
            idmglobal=int(idm_more[0])
            jdm_more=rgbf.readline().split()
            jdmglobal=int(jdm_more[0])
        # Usage: hycom_subset fin.a idm jdm i1 j1 idms jdms fout.a

        ex=exe(self.getexe('hafs_restart2restart'))
        restart_out='restart.%s.%s'%(cyc.strftime("%Y%m%d%H"),self.gridlabelout)
        cmd=ex[in_a,idmglobal,jdmglobal,self.ic,self.jc,self.idm,self.jdm,
               restart_out+'.a'] > restart_out+'.b.one'
        checkrun(cmd,logger=logger)
        deliver_file(restart_out+'.a',out_a,keep=True,logger=logger)

        (nc,wcb,twc)=(38,341,339)
        with open(out_b,'wt') as routf:
            with open(in_b,'rt') as globalf:
                line1=globalf.readline().strip('\r\n')
                line2=globalf.readline().strip('\r\n')
                routf.write('%s\n%s\n'%(line1,line2))
                del line1, line2
                with open(restart_out+'.b.one','rt') as regionf:
                    go=True
                    while go:
                        gline=globalf.readline()
                        if not gline or len(gline)<38:
                            break
                        gline=gline.strip('\r\n')
                        rline=regionf.readline().strip('\r\n')
                        splat=rline.replace('min, max =','').split()
                        if len(splat)<2:
                            break
                        oline='%s %16.7E%16.7E\n'\
                            %(gline[0:38],float(splat[0]),float(splat[1]))
                        routf.write(oline)

# Hycominit2 creates forcing for coupled run
class HYCOMInit2(hwrf.hwrftask.HWRFTask):
    def remove_ocean(): self.uncouple()

    def __init__(self,dstore,conf,section,taskname=None,fcstlen=126,
                 **kwargs):
        super(HYCOMInit2,self).__init__(dstore,conf,section,
                                       taskname=taskname,**kwargs)
        self.fcstlen=fcstlen
        self.make_products()
        self.spinlength=self.confint('spinlength',0)
        self.ic=None
        self.jc=None
        self.idm=None
        self.jdm=None
        self.ijgrid=None
        self.Application=None
        self.__rtofs_inputs=None
        self.__rtofs_inputs_ymd=None
        self.__blkdat=None

    @property
    def rtofs_inputs(self):
        if self.__rtofs_inputs is None:
            hd=self.confstr('catalog','hwrfdata')
            inputs=hwrf.input.DataCatalog(self.conf,hd,self.conf.cycle)
            self.__rtofs_inputs=inputs
        return self.__rtofs_inputs

    def make_products(self):
        """Initializes all Product objects to make them available to
        future jobs."""
        # Add the HyCOM-specific products whose delivery location is
        # in COM with the standard output file prefix
        # (invest99l.2017110318).
        logger=self.log()

        # forcing files for coupled run
        self.forcing_products=dict()
        ffiles=['airtmp','precip','presur','radflx','shwflx','surtmp',
                'tauewd','taunwd','vapmix','wndspd']
        for ffile in ffiles:
            for ab in 'ab':
                file='forcing.%s.%s'%(ffile,ab)
                comf=self.confstrinterp('{com}/{out_prefix}.'+file)
                prod=FileProduct(self.dstore,file,self.taskname,location=comf)
                prod.location=comf
                self.forcing_products[file]=prod
                logger.debug('%s => %s (%s)'%(file,comf,repr(prod))) 

        self.limits=FileProduct(
            self.dstore,'limits',self.taskname,location=
            self.confstrinterp('{com}/{out_prefix}.limits'))

        self.blkdat_input=FileProduct(
            self.dstore,'blkdat.input',self.taskname,location=
            self.confstrinterp('{com}/{out_prefix}.standalone.blkdat.input'))

    def fill_ocstatus(self,ocstatus,logger):
        """Fills the ocean status files with information.  This is
        called from exhwrf_ocean_init after a successful call to the
        run() function below.  The ocstatus argument is the
        hwrf.coupling.OceanStatus object that fills the files."""
        # First argument: True=run coupled, False=don't
        # Second argument: the logger argument sent to this function
        # Third argument: a list of extra lines to dump into fill_ocstatus
        ocstatus.set(self.run_coupled,logger,['forecast_exe=%s'%self.forecast_exe])

    def recall_ocstatus(self,ocstatus,logger):
        """Reads the ocean status back in during the forecast and
        check_init jobs, filling the self.run_coupled,
        self.forecast_exe variables."""
        # Get the name of the first ocstatus file for error reporting:
        for filename in ocstatus.fileiter():
            break

        # Read the lins of the ocstatus file:
        lines=ocstatus.read(logger)
        for line in lines:
            # See if any lines are KEY=VALUE lines, and split them into parts
            m=re.match('^ *([^ =]+) *= *(.*?) *$',line)
            if not m:
                logger.warning('%s: unparseable ocstatus line: %s'
                               %(filename,line))
                continue
            (key,value)=m.groups()
            value=value.strip()
            key=key.strip()

            # See if any recognized key=value lines are present:
            if key=='RUN_COUPLED':
                if value=='YES':     self.run_coupled=True
                elif value=='NO':    self.run_coupled=False
                else:
                    logger.warning('%s: ignoring unrecognized RUN_COUPLED value %s'%(
                            filename,repr(value)))
            elif key=='forecast_exe': self.forecast_exe=value
            else:
                logger.warning('%s: ignoring unknown key %s'%(filename,key))

    def make_forecast_forcing(self,logger):
        cyc=self.conf.cycle
        adjust_wind=0
        adjust_river=self.confint('adjust_river',0)
        adjust_temp=self.confint('adjust_temp',0)
        interval=self.confint('forecast_forcing_interval',3)
        startdate=self.conf.cycle
        fcstlen=self.fcstlen
        enddate=to_datetime_rel(fcstlen*3600,startdate)

        self.linkab('{FIXhycom}/hmon_{RUNmodIDout}.{gridlabelout}.regional.grid',
             'regional.grid')
        self.linkab('{FIXhycom}/hmon_{RUNmodIDout}.{gridlabelout}.regional.depth',
             'regional.depth')

        self.rtofs_get_forcing(startdate,enddate,interval*3600,adjust_river,
                               adjust_temp,adjust_wind,'fcst',logger)
        with open('limits','wt') as limitf:
            limitf.write('  %f %f false false  \n'%(
                    float(date_normal2hycom(cyc)),
                    float(date_normal2hycom(enddate))))

    def run(self):
        """Runs the hycom initialization for hycominit2.  Raises an exception if
        something goes wrong.  Returns on success."""
        logger=self.log()
        # Reset coupling status information:
        try:
            self.state=RUNNING
            # Inside the "with" block, we create and cd to the work
            # directory and then cd back out at the end.  The
            # "rm_first=True" means the directory is deleted first if
            # it already exists upon entry of the "with" block.
            with NamedDir(self.workdir,keep=not self.scrub,
                          logger=logger,rm_first=True) as d:
                self.select_domain(logger)

                # Create forcing for coupled run and raise an exception if it fails:
                self.make_forecast_forcing(logger)

                # Deliver the forcing files: 
                for (name,prod) in self.forcing_products.iteritems():
                    prod.deliver(frominfo='./'+name)

                self.limits.deliver(frominfo='./limits')

                # Make the flag file to indicate we're done.
                done=self.timestr('{com}/{vit[stnum]:02d}{vit[basin1lc]}.hycom2_init.done')
                with open(done,'wt') as f:
                    f.write('hycom2 init done for this cycle\n')

                # Make sure we run coupled:
                self.run_coupled=True
            self.state=COMPLETED
        except Exception as e:
            logger.error('Unhandled exception in ocean init: %s'
                         %(str(e),),exc_info=True)
            self.state=FAILED
            raise
        except:  # fatal signal, other catastrophic errors
            self.state=FAILED
            raise

    def inputiter(self):
        """!Iterates over all needed input data."""
        atmos1ds=self.confstr('atmos1_dataset')
        atmos2ds=self.confstr('atmos2_dataset')
        atmos1_flux=self.confstr('atmos1_flux')
        atmos1_grid=self.confstr('atmos1_grid')
        atmos2_flux=self.confstr('atmos2_flux')
        atmos2_grid=self.confstr('atmos2_grid')
        oceands=self.confstr('ocean_dataset')
        ocean_past=self.confstr('ocean_past')
        ocean_now=self.confstr('ocean_now')
        ocean_rst=self.confstr('ocean_rst')
        ocean_fcst=self.confstr('ocean_fcst')
        spinlength=self.confint('spinlength',24)
        logger=self.log()
        hwrfatime=self.conf.cycle
        hwrf_start=-24
        hwrf_finish=int(math.ceil(float(self.fcstlen)/3)*3)
        fcstint=self.confint('forecast_forcing_interval',3)

        # Request GDAS data from -25 to +1
        assert(atmos1ds == 'gdas1')
        for h in xrange(-spinlength-1,2):
            ahr= (h-1)//6.0 * 6  # 6 is the GDAS cycling interval
            atime=to_datetime_rel(ahr*3600,hwrfatime)
            ftime=to_datetime_rel(h*3600,hwrfatime)
            assert(ftime>atime)
            yield dict(dataset=atmos1ds,item=atmos1_flux,
                       atime=atime,ftime=ftime)
            yield dict(dataset=atmos1ds,item=atmos1_grid,
                       atime=atime,ftime=ftime)
            logger.info('hycom inputiter, request flux and master from %s at atime=%s ftime=%s'%(atmos1ds,atime.strftime("%Y%m%d%H"),ftime.strftime('%Y%m%d%H')))
            del atime, ftime, ahr
        del h

        # Request GFS data from -3 to hwrf_finish+3
        assert(atmos2ds == 'gfs')
        for h in xrange(-fcstint,hwrf_finish+fcstint*2,fcstint):
            ftime=to_datetime_rel(h*3600,hwrfatime)
            if h<=0:
                ahr= (h-1)//6.0 * 6  # 6 is the GFS cycling interval
                atime=to_datetime_rel(ahr*3600,hwrfatime)
                logger.info('negative h=%d with ahr=%d atime=%s'%(
                        h,ahr,atime.strftime('%Y%m%d%H')))
            else:
                atime=hwrfatime
                logger.info('positive h=%d atime=%s'%(h,atime.strftime('%Y%m%d%H')))
            yield dict(dataset=atmos2ds,item=atmos2_flux,
                       atime=atime,ftime=ftime)
            yield dict(dataset=atmos2ds,item=atmos2_grid,
                       atime=atime,ftime=ftime)
            logger.info('hycom inputiter, request flux and master from '
                        '%s at h=%d atime=%s ftime=%s'%(
                    atmos2ds,h,atime.strftime("%Y%m%d%H"),
                    ftime.strftime('%Y%m%d%H')))
        del ftime, atime, ahr, h

        # Assume 24 hour cycle for ocean model.

        # oceanatime is today's RTOFS analysis time as a datetime:
        todayatime=datetime.datetime(
            hwrfatime.year,hwrfatime.month,hwrfatime.day)

        # prioratime is yesterday's RTOFS analysis time as a datetime:
        prioratime=to_datetime_rel(-24*3600,todayatime)

        # Last allowed lead time today:
        last_fhr_today=self.last_lead_time_today(hwrfatime.hour)

        # Epsilon for time comparisons:
        epsilon=to_fraction(900) # 15 minutes

        # Loop over all forecast hours from -24 to the end of the HWRF
        # forecast, relative to the HWRF analysis time.  We round up
        # to the next nearest 3 hours since the RTOFS outputs
        # three-hourly:

        logger.info("In hycom inputiter, todayatime=%s prioratime=%s "
                    "epsilon=%s hwrf_start=%s hwrf_finish=%s "
                    "last_fhr_today=%s"%(
                repr(todayatime),repr(prioratime),repr(epsilon),
                repr(hwrf_start),repr(hwrf_finish),repr(last_fhr_today)))

        yield dict(dataset=oceands,item=ocean_rst,
                   atime=prioratime,ftime=prioratime,ab='a')
        yield dict(dataset=oceands,item=ocean_rst,
                   atime=prioratime,ftime=prioratime,ab='b')

        for fhr in xrange(hwrf_start,hwrf_finish+3,3):

            # The ftime is the desired forecast time as a datetime:
            ftime=to_datetime_rel(fhr*3600,hwrfatime)

            # Which RTOFS cycle do we use?
            if fhr>last_fhr_today:
                oceanatime=prioratime
            else:
                oceanatime=todayatime

            # The oceanftime is the forecast lead time for TODAY's
            # RTOFS as a datetime.timedelta.
            oceanftime=ftime-oceanatime

            # The oceanfhr is the RTOFS forecast hour as an integer
            # for TODAY's RTOFS forecast.
            oceanfhr=int(round(to_fraction(oceanftime,negok=True)))

            logger.info('fhr=%s ftime=%s oceanatime=%s oceanftime=%s oceanfhr: to_fraction=%s round=%s int=%s'%(
                    repr(fhr),repr(ftime),repr(oceanatime),repr(oceanftime),
                    repr(to_fraction(oceanftime,negok=True)),
                    repr(round(to_fraction(oceanftime,negok=True))),
                    repr(oceanfhr)))

            # Nowcasts are always from current RTOFS cycle since
            # they're available before HWRF 0Z starts.
            if oceanfhr<0:
                logger.info('For fhr=%s oceanfhr=%s use ocean_past atime=%s ftime=%s'%(
                        repr(fhr),repr(oceanfhr),repr(oceanatime),repr(ftime)))
                yield dict(dataset=oceands,item=ocean_past,
                           atime=oceanatime,ftime=ftime,ab='a')
                yield dict(dataset=oceands,item=ocean_past,
                           atime=oceanatime,ftime=ftime,ab='b')
                continue
            elif oceanfhr==0:
                logger.info('For fhr=%s oceanfhr=%s use ocean_now atime=%s ftime=%s'%(
                        repr(fhr),repr(oceanfhr),repr(oceanatime),repr(ftime)))
                yield dict(dataset=oceands,item=ocean_now,
                           atime=oceanatime,ftime=ftime,ab='a')
                yield dict(dataset=oceands,item=ocean_now,
                           atime=oceanatime,ftime=ftime,ab='b')
                yield dict(dataset=oceands,item=ocean_rst,
                           atime=oceanatime,ftime=ftime,ab='a')
                yield dict(dataset=oceands,item=ocean_rst,
                           atime=oceanatime,ftime=ftime,ab='b')
                continue

            # Later times' availability depends on HWRF forecast
            # cycle:
            #   HWRF 00Z => RTOFS today available through 00Z
            #   HWRF 06Z => RTOFS today available through +96 hours
            #   HWRF 12Z => RTOFS today available through +192 hours
            #   HWRF 18Z => RTOFS today available through +192 hours

            chosen_atime=oceanatime
            if oceanfhr<=96 and hwrfatime.hour<6:
                chosen_atime=prioratime
            if oceanfhr<96 and hwrfatime.hour<12:
                chosen_atime=prioratime

            logger.info('For fhr=%s oceanfhr=%s use ocean_fcst atime=%s ftime=%s'%(
                    repr(fhr),repr(oceanfhr),repr(chosen_atime),repr(ftime)))
            yield dict(dataset=oceands,item=ocean_fcst,
                       atime=chosen_atime,ftime=ftime,ab='a')
            yield dict(dataset=oceands,item=ocean_fcst,
                       atime=chosen_atime,ftime=ftime,ab='b')

    def select_domain(self,logger):
        atmos_lon=self.conffloat('domlon','cenlo',section='config')
        # Fit lon within [-180,180)
        atmos_lon=(3600+int(round(atmos_lon))+180)%360-180
        basin=self.storminfo.pubbasin2
        nhbasin = basin in ('AL', 'EP', 'CP', 'WP', 'IO')
        Application=None
        if basin=='AL':
            Application='hat10_basin'
        elif basin=='CP':
            Application='hcp70_basin'
        elif nhbasin and atmos_lon>-180 and atmos_lon<=20:
            Application='hep20_basin'
        elif nhbasin and atmos_lon>100:
            Application='hwp30_basin'
        elif nhbasin and atmos_lon>20 and atmos_lon<=100:
            Application='hin40_basin'
        elif basin in [ 'SL', 'LS' ]:
            Application='hsn50_basin'
        elif basin in [ 'SH', 'SP', 'SI' ]:
            Application='hsp60_basin'
        else:
            msg='No ocean basin available for basin=%s lat=%s.  Run uncoupled.'%(
                basin,repr(atmos_lon))
            jlogger.warning(msg)
            raise hwrf.exceptions.NoOceanBasin(msg)
        self.RUNmodIDin='rtofs_glo'
        RUNmodIDin=self.RUNmodIDin
        
        aptable=self.confstrinterp('{PARMhycom}/hmon_rtofs.application_table')
        found=False
        with open(aptable,'rt') as apfile:
            for line in apfile:
                fields=line.split()
                if len(fields)<9:
                    logger.info('%s: ignore line %s'%(aptable,line.strip()))
                    continue
                (ap,inmod,outmod,gridid,proc,np1,np2,mp1,mp2) = fields
                if ap==Application and inmod==self.RUNmodIDin:
                    found=True
                    self.RUNmodIDout=outmod
                    self.gridid=gridid
                    found=True
        del line,apfile
        if not found:
            msg='%s: could not find Application=%s RUNmodIDin=%s'%(
                aptable,repr(Application),repr(self.RUNmodIDin))
            logger.error(msg)
            raise hwrf.exceptions.InvalidOceanInitMethod(msg)

        gridtable=self.confstrinterp('{PARMhycom}/hmon_rtofs.grid_table')
        found=False
        with open(gridtable,'rt') as gridfile:
            for line in gridfile:
                fields=line.split()
                if len(fields)<11:
                    logger.info('%s: ignore line %s'%(gridtable,line.strip()))
                    continue
                (ingridid,gridlabelin,gridlabelout,grid_source,\
                     idm,jdm,kdm,ic,jc,ijgrid,gridno) = fields
                idm=int(idm)
                jdm=int(jdm)
                kdm=int(kdm)
                ic=int(ic)
                jc=int(jc)
                ijgrid=int(ijgrid)
                gridno=int(gridno)
                if ingridid==self.gridid and line.find(RUNmodIDin)>=0:
                    found=True
                    ( self.idm, self.jdm, self.kdm, self.kkdm ) = \
                     (     idm,      jdm,      kdm,      kdm  )
                    ( self.ic, self.jc, self.ijgrid, self.gridno ) = \
                     (     ic,      jc,      ijgrid,      gridno )
                    ( self.gridlabelin, self.gridlabelout ) = \
                     (     gridlabelin,      gridlabelout )
        if not found:
            msg='%s: could not find grid=%s RUNmodIDin=%s'%(
                gridtable,repr(self.gridid),repr(RUNmodIDin))
            logger.error(msg)
            raise hwrf.exceptions.InvalidOceanInitMethod(msg)
        assert(self.section=='hycominit2')
        self.conf.set(self.section,'RUNmodIDin',self.RUNmodIDin)
        self.conf.set(self.section,'gridlabelin',self.gridlabelin)
        self.conf.set(self.section,'gridlabelout',self.gridlabelout)
        self.conf.set(self.section,'RUNmodIDout',self.RUNmodIDout)
        with open('hycom_settings','wt') as f:
            f.write('''export idm={idm}
export jdm={jdm}
export kdm={kdm}
export kkdm={kkdm}
export ic={ic}
export jc={jc}
export ijgrid={ijgrid}
export gridlabelout={gridlabelout}
export gridlabelin={gridlabelin}
export RUNmodIDout={RUNmodIDout}
export RUNmodIDin={RUNmodIDin}
export gridno={gridno}\n'''.format(**self.__dict__))

        logger.info('HYCOM grid: i,j,kdm=%d,%d,%d ic,jc=%d,%d ijgrid=%d gridno=%d'%(
                idm,jdm,kdm,ic,jc,ijgrid,gridno))
        jlogger.info('HYCOM domain: in=%s.%s out=%s.%s'%(
                self.RUNmodIDin,self.gridlabelin,
                self.RUNmodIDout,self.gridlabelout))

# getges1 - if cannot find file then go back to previous cycle
    def getges1(self,atmosds,grid,time):
        logger=self.log()
        sixhrs=to_timedelta(6*3600)
        epsilon=to_timedelta(30)
        atime0=self.conf.cycle
        atime=atime0
        rinput=self.rtofs_inputs
        glocset=0
        for itry in xrange(0,-10,-1):
            if time>atime+epsilon:

                gloc=rinput.locate(atmosds,grid,atime=atime,ftime=time)
                logger.info('Looking for: %s - %s'%(repr(itry),repr(gloc)))
                if glocset==0:
                   gloc0=rinput.locate(atmosds,grid,atime=atime,ftime=time)
                   glocset=1

                if isnonempty(gloc):
                    logger.info('%s %s %s => %s'%(
                            repr(atmosds),repr(grid),repr(time),
                            repr(gloc)))
                    return (gloc)
                if itry<=-9:
                    if wait_for_files([gloc],logger=logger,maxwait=60,sleeptime=5):
                       logger.info('%s %s %s => %s'%(
                            repr(atmosds),repr(grid),repr(time),
                            repr(gloc)))
                       return (gloc)
                    else:
                       logger.warning('%s : do not exist or empty'%(gloc))
            else:
                logger.warning('%s<=%s+%s'%(repr(time),repr(atime),repr(epsilon)))
            atime=atime-sixhrs
        msg='Cannot find file for time %s; first file tried %s'%(time.strftime('%Y%m%d%H'),gloc0)
        self.log().error(msg)
        raise hwrf.exceptions.NoOceanData(msg)


# seasforce4 (init2) - 
#              calls gfs2ofs in mpmd mode. There are 10 instances of gfs2ofs
#              and it creates 10 forcing files.
    def ofs_seasforce4(self,date1,date2,mode,logger):
        if mode=='anal':
            ihours=1
            atmosds=self.confstr('atmos1_dataset')
            atmos_flux=self.confstr('atmos1_flux')
            atmos_grid=self.confstr('atmos1_grid')
        else:
            ihours=3
            atmosds=self.confstr('atmos2_dataset')
            atmos_flux=self.confstr('atmos2_flux')
            atmos_grid=self.confstr('atmos2_grid')
        cyc=self.conf.cycle
        hourstep=to_timedelta(ihours*3600)
        epsilon=to_timedelta(30)
        nm=to_fraction(date2-date1)/(3600*ihours) + 3
        listflx=['%d\n'%int(round(nm))]
        rdate1=date1-hourstep
        rdate2=date2+hourstep
        stopdate=rdate2+epsilon

        wgrib2=alias(exe(self.getexe('wgrib2')))
        grb2index=alias(exe(self.getexe('grb2index')))
        wgrib2loc=self.getexe('wgrib2')
        grb2indexloc=self.getexe('grb2index')

        if mode=='anal':
            TYPEx='hour fcst'
            TYPEx='ave'
        else:
            TYPEx='ave'

        fields=[
            {"FLUX":'UFLX',   "LEVEL":'surface',           "TYPE":'ave'   },
            {"FLUX":"VFLX",   "LEVEL":"surface",           "TYPE":"ave"   },
            {"FLUX":"TMP",    "LEVEL":'2 m above ground',  "TYPE":"fcst"  },
            {"FLUX":"SPFH",   "LEVEL":'2 m above ground',  "TYPE":"fcst"  },
            {"FLUX":"PRATE",  "LEVEL":"surface",           "TYPE":"ave"   },
            {"FLUX":"UGRD",   "LEVEL":'10 m above ground', "TYPE":"fcst"  },
            {"FLUX":"VGRD",   "LEVEL":'10 m above ground', "TYPE":"fcst"  },
            {"FLUX":"SHTFL",  "LEVEL":"surface",           "TYPE":TYPEx   },
            {"FLUX":"LHTFL",  "LEVEL":"surface",           "TYPE":TYPEx   },
            {"FLUX":"DLWRF",  "LEVEL":"surface",           "TYPE":TYPEx   },
            {"FLUX":"ULWRF",  "LEVEL":"surface",           "TYPE":TYPEx   },
            {"FLUX":"DSWRF",  "LEVEL":"surface",           "TYPE":TYPEx   },
            {"FLUX":"USWRF",  "LEVEL":"surface",           "TYPE":TYPEx   },
            {"FLUX":"TMP",    "LEVEL":"surface",           "TYPE":"fcst"  },
            {"FLUX":"PRES",   "LEVEL":"surface",           "TYPE":"fcst"  },
            {"FLUX":"LAND",   "LEVEL":"surface",           "TYPE":"fcst"  },
            {"FLUX":"PRMSL",  "LEVEL":"sea level",         "TYPE":"fcst"  }
            ]

        datei=rdate1
        inputs=self.rtofs_inputs
        cmd=self.getexe('gfs2ofsinputs.py')
        commands=list()
        while datei<stopdate:
            sf=self.getges1(atmosds,atmos_grid,datei)
            flxfile=datei.strftime("%Y%m%d%H")+'.sfcflx'
            remove_file(flxfile,info=True,logger=logger)

            # Subset flux file:
            make_symlink(sf,flxfile+'.in2',logger=logger,force=True)

#START
#           sfindex=runstr(wgrib2[flxfile+'.in2'],logger=logger)
#           reindex=''
#           for flt in fields:
#               for line in sfindex.splitlines():
#                   if line.find(flt['FLUX']) >=0 and \
#                      line.find(flt['LEVEL']) >=0 and \
#                      line.find(flt['TYPE']) >=0:
#                       reindex+=line+'\n'
#                       logger.info('%s: keep(sf): %s'%(
#                               flxfile+'.in2',line.strip()))
#           logger.info('KEEP(SF):\n'+reindex)
#           checkrun(wgrib2[flxfile+'.in2',"-i",'-grib',flxfile+'.in3']
#                    << reindex,logger=logger)
#           checkrun(wgrib2[flxfile+'.in3',"-new_grid_winds","earth",
#              "-new_grid","gaussian","0:1440:0.25","89.75:720",flxfile+'.in4']
#              ,logger=logger)
#           deliver_file(flxfile+'.in4',flxfile,keep=True,logger=logger)
#           checkrun(grb2index[flxfile,flxfile+'.idx'])
#END

            listflx.append('%s %s %s %s %s\n'%(
                    datei.strftime('%Y%m%d%H'),flxfile,'none',sf,'none'))
            g2oinputs_out='gfs2outputs.%s.out'%datei.strftime('%Y%m%d%H')
            commands.append('%s %s %s %s %s < /dev/null > %s 2>&1\n'%(
                    cmd,mode,flxfile,wgrib2loc,grb2indexloc,g2oinputs_out))

            datei+=hourstep
        # end while datei<stopdate

        with open('command.file.preview','wt') as cfpf:
            cfpf.write(''.join(commands))

        tt=int(os.environ['TOTAL_TASKS'])
        logger.info ('CALLING gfs2ofsinputs %d ',tt)
	mpiserial_path=os.environ.get('MPISERIAL','*MISSING*')
	if mpiserial_path=='*MISSING*':
             mpiserial_path=produtil.fileop.find_exe('mpiserial')
# or mpiserial_path='/path/to/where/dan/has/mpiserial'
#        cmd2=mpirun(mpi(mpiserial)['-m','command.file.preview'],allranks=True)
        cmd2=mpirun(mpi(mpiserial_path)['-m','command.file.preview'],allranks=True)
        checkrun(cmd2)


        with open('listflx.dat','wt') as listflxf:
            listflxf.write(''.join(listflx))
        with open('intp_pars.dat','wt') as ippdf:
            ippdf.write('''
&intp_pars
avstep = %d.,      ! averaging fluxes (in hours)
mrffreq = %d.,     ! frequency of MRF fluxes (in hours)  = mrffreq for no averaging
flxflg = 15,       ! Type of HYCOM input (=4 => nhycom=7 and =5 => nhycom=8)
dbgn = 0,         ! debugging =0 - no dbg; =1,2,3 - add output
avg3 = 2,         ! if avg3 = 1, then averaged fluxes are converted to instantaneous fields
wslocal = 0       ! if  wslocal = 1, then wind stress are computed from wind velcoities
/
''' %(ihours,ihours))
# echo "8 8 0 0 8 0 0 0 0 8 8 8 8 0 0 0" > jpdt_table.dat
        with open('jpdt_table.dat','wt') as j:
            j.write('''8 8 0 0 8 0 0 0 0 8 8 8 8 0 0 0''')
        #hmon_gfs2ofs=exe(self.getexe('hmon_gfs2ofs'))
        hafs_gfs2ofs=alias(batchexe(self.getexe('hafs_gfs2ofs2')))
        commands=list()
        for i in xrange(1,11):
          gfs2ofs_in='gfs2ofs.%d.in'%i
          gfs2ofs_out='gfs2ofs.%d.out'%i
          with open (gfs2ofs_in,'wt') as f:
              f.write("""%d\n"""%(i))
          commands.append ( (hafs_gfs2ofs<gfs2ofs_in)>gfs2ofs_out )
        logger.info ('COMMANDS %s '%repr(commands))
        bigcmd=mpiserial(commands[0])
        tt=int(os.environ['TOTAL_TASKS'])
        for c in commands[1:]:
            bigcmd+=mpiserial(c)
        ttotal=(len(commands))
        for i in xrange(ttotal,tt):
            bigcmd+=mpiserial(batchexe('/bin/true'))

        logger.info ('BIGCMD %s '%repr(bigcmd))

#        checkrun(hmon_gfs2ofs,logger=logger)
        checkrun(mpirun(bigcmd),logger=logger)
        self.ofs_timeinterp_forcing(logger)

    def ofs_forcing_info(self,filename):
        num_times=0
        blines=0
        aname=None
        with open(filename,'rt') as inf:
            start=inf.tell()
            for line in inf:
                if aname is None and line.find('span')>=0:
                    aname=line[0:10]
                    break
            if aname is None:
                msg='%s: could not find data records in file.'%(filename,)
                raise hwrf.exceptions.OceanDataInvalid(msg)
            inf.seek(start)
            for line in inf:
                blines+=1
                if line.find(aname):
                    num_times+=1
        return (blines,num_times,blines-num_times,aname)

    def ofs_timeinterp_forcing(self,logger):
        with produtil.cd.NamedDir('temp',logger=logger,rm_first=True):
            for ofield in [ 'airtmp','precip','presur','radflx','shwflx',
                            'surtmp','tauewd','taunwd','vapmix','wndspd']:
                for ab in 'ab':
                    ffrom='../forcing.'+ofield+'.'+ab
                    if os.path.exists(ffrom):
                        fto='forcing.'+ofield+'.'+ab
                        logger.info('Move %s => %s'%(ffrom,fto))
                        os.rename(ffrom,fto)
        # End moving of old forcing to temp/
        
        ofs_timeinterp_forcing=alias(mpirun(mpi(self.getexe(
                    'hafs_timeinterp_forcing'))))

        xfield=[
            # First step, precip => [airtemp, ...]
            ['precip',['airtmp','presur','surtmp','vapmix','wndspd'] ],
            # Second step: airtmp => [precip, ...]
            ['airtmp',['precip','tauewd','taunwd','radflx','shwflx'] ]
            ]

        for ifield,ofields in xfield:
            ifile='temp/forcing.%s.b'%(ifield,)
            (_,num_times,ihead,_)=self.ofs_forcing_info(ifile)

            for ofield in ofields:
                tfile='timeinterp_forcing.%s.in'%(ofield,)
                ofile='temp/forcing.%s.b'%(ofield,)
                (_,num_frames,ohead,sname)=self.ofs_forcing_info(ofile)
                # wrong order: inputs=[ifield,ihead,num_times,ofield,ohead,num_frames,sname]
                inputs=[ifield,num_times,ihead,ofield,num_frames,ohead,sname]
                inputs=[str(i) for i in inputs]
                inputs='\n'.join(inputs) + '\n'
                logger.info('%s: write %s'%(tfile,repr(inputs)))
                with open(tfile,'wt') as tf:
                    tf.write(inputs)
                checkrun(ofs_timeinterp_forcing<tfile,logger=logger)
        # end for ifield,ofields in xfield

    def ofs_correct_forcing(self,logger):
        (_,num_frames,ihead,sname)=self.ofs_forcing_info('forcing.surtem.b')
        (_,_,_,aname)=self.ofs_forcing_info('forcing.airtmp.b')
        inputs=[ihead,num_frames,aname,sname]
        inputs='\n'.join([str(i) for i in inputs]) + '\n'
        with open('correct_forcing.in','wt') as cfif:
            cfif.write(inputs)
        self.moveab('forcing.airtmp','forcing.airtm1')
        hafs_correct_forcing=self.getexe('hafs_correct_forcing')
        checkrun(exe(hafs_correct_forcing)<'correct_forcing.in',logger=logger)

    def blkflag(self,flagname,default=NO_DEFAULT):
        blkdat=self.blkdat
        if  default is not NO_DEFAULT and \
                flagname not in blkdat:
            return default
        return abs(self.blkdat[flagname][0])>.01

    def rtofs_get_forcing(self,srtdate,enddate,intvl,adjust_river,
                          adjust_temp,adjust_wind,mode,logger):
        priver=self.blkflag('priver',False)
        flxflg=self.blkflag('flxflg',False)
        wndflg=self.blkflag('wndflg',False)
        atpflg=self.blkflag('atpflg',False)
        logger.info('ANOTHER STRING FOR WHICH TO GREP - mode=%s adjust_temp=%s adjust_wind=%s '%(
		repr(mode),repr(adjust_river),repr(adjust_wind)))
        forcingfilesmade=-1

        # Atmospheric forcing
        if not flxflg and not wndflg and not atpflg:
            logger.info('No atmospheric forcing created')
        else:
            forcingfilesmade=0
            logger.info('Create atmospheric forcing.')
            deliver_file(self.icstr('{FIXhycom}/ofs_atl.ismus_msk1760x880.dat'),'ismus_msk1760x880.dat',keep=True,logger=logger)
            deliver_file(self.icstr('{FIXhycom}/ofs_atl.ismus_msk3072x1536.dat'),'ismus_msk3072x1536.dat',keep=True,logger=logger)
            deliver_file(self.icstr('{FIXhycom}/ofs_atl.ismus_msk1440x721.dat'),'ismus_msk1440x721.dat',keep=True,logger=logger)
            deliver_file(self.icstr('{FIXhycom}/ofs_atl.ismus_msk1440x720.dat'),'ismus_msk1440x720.dat',keep=True,logger=logger)
            self.ofs_seasforce4(srtdate,enddate,mode,logger)
            if adjust_temp:
                self.ofs_correct_forcing(logger)
        # end if not flxflg and not wndflg and not atpflg

        # River forcing:
        if priver>0:
            self.copyab('{FIXhycom}/hmon_{RUNmodIDout}.{gridlabelout}.forcing.'
                   'rivers','forcing.rivers')
            logger.info('Station River Forcing Files copied')
            forcingfilesmade=0
        else:
            logger.info('River Forcing Files are not employed')

        # Wind forcing:
        if adjust_wind>0:
            with open('../tmpvit','rt') as t:
                splat=t.readline().split()
                depth=splat[18]
            if depth=='S':
                logger.info('Hurricane too shallow to run parameterized winds')
            else:
                self.ofs_windforcing(logger)
                if adjust_wind>1:
                    renameme=[
                        ['forcing.wndspd.a', 'forcing.wndspd.a.original'],
                        ['forcing.wndspd.b', 'forcing.wndspd.b.original'],
                        ['forcing.tauewd.a', 'forcing.tauewd.a.original'],
                        ['forcing.tauewd.b', 'forcing.tauewd.b.original'],
                        ['forcing.taunwd.a', 'forcing.taunwd.a.original'],
                        ['forcing.taunwd.b', 'forcing.taunwd.b.original'],
                        ['forcing.wdspd1.a', 'forcing.wndspd.a'],
                        ['forcing.wdspd1.b', 'forcing.wndspd.b'],
                        ['forcing.tauew1.a', 'forcing.tauewd.a'],
                        ['forcing.tauew1.b', 'forcing.tauewd.b'],
                        ['forcing.taunw1.a', 'forcing.taunwd.a'],
                        ['forcing.taunw1.b', 'forcing.taunwd.b'],
                        ]
                    for (ifrom,ito) in renameme:
                        logger.info('Rename %s to %s'%(ifrom,ito))
                        os.rename(ifrom,ito)

        return forcingfilesmade

    def copyab(self,sfrom,fto):
        ffrom=self.timestr(sfrom)
        produtil.fileop.deliver_file(
            ffrom+'.a',fto+'.a',keep=True,logger=self.log())
        produtil.fileop.deliver_file(
            ffrom+'.b',fto+'.b',keep=True,logger=self.log())

    def moveab(self,ffrom,fto):
        deliver_file(ffrom+'.a',fto+'.a',keep=False,logger=self.log())
        deliver_file(ffrom+'.b',fto+'.b',keep=False,logger=self.log())

    def linkab(self,sfrom,fto):
        ffrom=self.timestr(sfrom)
        produtil.fileop.make_symlink(
            ffrom+'.a',fto+'.a',force=True,logger=self.log())
        produtil.fileop.make_symlink(
            ffrom+'.b',fto+'.b',force=True,logger=self.log())

    @property
    def blkdat(self):
        if self.__blkdat is not None: return self.__blkdat
        d=collections.defaultdict(list)
        self.__blkdat=d
        blkdat_input=self.timestr('{PARMhycom}/hmon_{RUNmodIDout}.{gridlabelout}.fcst.blkdat.input')
        with open(blkdat_input) as f:
            for line in f:
                m=re.match("^\s*(\S+)\s*'\s*(\S+)\s*' = ",line)
                if m:
                    (val,kwd)=m.groups(0)
                    val=float(val)
                    d[kwd].append(val)
        return self.__blkdat

    @property
    def baclin(self):
        return self.blkdat['baclin'][0]

class HYCOMIniter(hwrf.coupling.ComponentIniter):
    def __init__(self,hycomfcst,ocstatus):
        self.hycomfcst=hycomfcst
        self.ocstatus=ocstatus
    @property
    def hycominit1(self):
        return self.hycomfcst.hycominit

    def check_coupled_inputs(self,logger):
        """This subroutine is run by the check_init job and checks to
        see if the initialization has succeeded.  It returns True if
        the inputs are all present, and False if they're not."""
        hf=self.hycomfcst
        hi=hf.hycominit
        hi.recall_ocstatus(self.ocstatus,logger)
        if not hi.run_coupled:
            logger.warning('Hycom init says we will not run coupled.')
            return True
        # We get here if we run coupled.  The HYCOMInit.run() function
        # always makes the init_file1 and init_file2 products:
        prods=list()

        # Check A files:
        for (ftime,prod) in hi.init_file2a.iteritems():
            prods.append(prod)
        # Check B files
        for (ftime,prod) in hi.init_file2b.iteritems():
            prods.append(prod)
        # Check restart files
        # add restart files
        # Check forcing files
        # add forcing files

        count=0
        for prod in prods:
            if not prod.available:
                logger.error('%s: product not available'%(prod.did,))
            elif not prod.location:
                logger.error('%s: no path set in database'%(prod.did,))
            elif not os.path.exists(prod.location):
                logger.error('%s: %s: file does not exist'%(
                        prod.did,prod.location))
            else:
                logger.info('%s: %s: product is delivered'%(
                        prod.did,prod.location))
                count+=1
        if count<len(prods):
            logger.error('Have only %d of %d products.  Ocean init failed.'%(
                    count,len(prods)))
            return False
        else:
            logger.info('Rejoice: we have all coupled inputs')
            return True

    def link_coupled_inputs(self,just_check,logger):
        """Called from the forecast job.  If just_check=True, this
        calls check_coupled_inputs.  Otherwise, this links all hycom
        inputs to the local directory."""
        if just_check:
            return self.check_coupled_inputs(logger)
        hi=self.hycomfcst.hycominit
        hi.recall_ocstatus(self.ocstatus,logger)
        if not hi.run_coupled:
            logger.warning('Hycom init says we will not run coupled.')
            return True

        # List of product object to link/copy:
        prods=list()

        # A files:
        for (ftime,prod) in hi.init_file2a.iteritems():
            prods.append(prod)
        # B files
        for (ftime,prod) in hi.init_file2b.iteritems():
            prods.append(prod)

        # Make the subdirectories
        if not just_check:
            produtil.fileop.makedirs('nest',logger=logger)
            produtil.fileop.makedirs('incup',logger=logger)

        # Now link the inputs:
        cycle=self.hycominit.conf.cycle
        for prod in prods:
            if not prod.available or not prod.location or \
                    not os.path.exists(prod.location):
                msg='%s: input not present (location=%s available=%s)'\
                    %(prod.did, repr(prod.location), repr(prod.available))
                logger.error(msg)
                raise hwrf.exceptions.OceanInitFailed(msg)
            m=re.match('hmon_basin.0*(\d+).([ab])',prod.prodname)
            if not m:
                make_symlink(prod.location,prod.prodname,
                             logger=logger,force=True)
            else:
                (hr,ab)=m.groups()
                hr=int(hr)
                hycomtime=to_datetime_rel(-24*3600,cycle)
                logger.info('Link hour %s relative to cycle %s.'%(repr(hr),repr(hycomtime)))
                t=hwrf.numerics.to_datetime_rel(hr*3600,hycomtime)
                name1=t.strftime('nest/archv.%Y_%j_%H.')+ab
                name2=t.strftime('incup/incupd.%Y_%j_%H.')+ab
                for name in ( name1, name2 ):
                    make_symlink(prod.location,name,
                                 logger=logger,force=True)

        # link restart file (bill02l.2015061600.rtofs_hat10.restart.[ab])
        op=hi.timestr('{out_prefix}.')
        nop=len(op)
        assert(nop>5)
        for part in [ 'rtofs','limits','forcing' ]:
            globby=hi.timestr('{com}/{out_prefix}.{part}*',part=part)
            nfound=0
            # Path will be something like:
            # /path/to/com/2015061600/02L/bill02l.2015061600.rtofs_hat10.restart.a
            # ipref is index of the "." after 2015061600
            # localname is rtofs_hat10.restart.a
            # finalname is hat10.restart_in.a
            for path in glob.glob(globby):
                nfound+=1
                ipref=path.rfind(op)
                localname=path[ipref+nop:]
                # Rename restart files:
                finalname=re.sub('.*\.restart\.([^/]*)','restart_in.\\1',localname)
                make_symlink(path,finalname,force=True,logger=logger)
            logger.info('%s: %d files linked\n',globby,nfound)
            assert(nfound>0)
    
	if not just_check:
            hsprod=self.hycominit.hycom_settings
            assert(hsprod is not None)
            assert(hsprod.available)
            assert(hsprod.location)
            RUNmodIDout=read_RUNmodIDout(hsprod.location)
            self.link_hycom_fix(RUNmodIDout)
            self.link_hycom_parm(RUNmodIDout)
        return True

    def link_hycom_parm(self,RUNmodIDout):
        logger=self.hycominit.log()
        mine={ 'fcst.blkdat.input':'blkdat.input',
               'patch.input.90':'patch.input',
               'ports.input':'ports.input' }
        for (parmbase,localname) in mine.iteritems():
            parmfile=self.hycominit.timestr(
                '{PARMhycom}/hmon_{RUNmodIDout}.basin.{PARMBASE}',
                RUNmodIDout=RUNmodIDout,PARMBASE=parmbase)
            produtil.fileop.make_symlink(parmfile,localname,logger=logger,force=True)

    def link_hycom_fix(self,RUNmodIDout):
        assert(RUNmodIDout)
        logger=self.hycominit.log()
        globby=self.hycominit.timestr('{FIXhycom}/hmon_{RUNmodIDout}.basin.*',
                          RUNmodIDout=RUNmodIDout)
        forcewanted=set(['chl.a','chl.b','offlux.a','offlux.b','rivers.a','rivers.b'])
        n=0
        nlinked=0
        for path in glob.glob(globby):
            basename=os.path.basename(path)
            fd=basename.find('forcing')
            linked=False
            n+=1
            if fd<0:
                bd=basename.find('basin.')
                if bd>0:
                    produtil.fileop.make_symlink(path,basename[(bd+6):],logger=logger,force=True)
                    linked=True
            else:
                forcetype=basename[(fd+8):]
                if forcetype in forcewanted:
                    produtil.fileop.make_symlink(path,'forcing.'+forcetype,logger=logger,force=True)
                    linked=True
            if not linked:
                logger.info('%s: not linking %s'%(basename,path))
            else:
                nlinked+=1
        logger.info('Linked %d of %d HyCOM fix files for RUNmodIDout=%s'%(
                    nlinked,n,repr(RUNmodIDout)))
        produtil.fileop.make_symlink('../relax.rmu.a','nest/rmu.a',logger=logger,force=True)
        produtil.fileop.make_symlink('../relax.rmu.b','nest/rmu.b',logger=logger,force=True)

    def make_exe(self,task,exe,ranks):
        """Returns an MPIRanksBase to run the executable chosen by the
        initialization.  This function must only be called after
        link_coupled_inputs"""
        if not isinstance(ranks,int):
            raise TypeError('The ranks argument to make_exe must be an int.  You provided a %s %s'%(type(ranks).__name__,repr(ranks)))
        task.read_hycom_init_vars()
        assert(task.tvhave('RUNmodIDout'))
        exec_name=task.icstr('hafs_{RUNmodIDout}_forecast')
        wantexe=task.getexe(exec_name)
#        wantexe=task.hycominit.forecast_exe
        if not wantexe:
            raise hwrf.exceptions.OceanExeUnspecified(
                'The forecast_exe option was not specified in the '
                'ocean status file.')
        return mpi(wantexe)*ranks

class WRFCoupledHYCOM(hwrf.coupling.CoupledWRF):
    """This subclass of CoupledWRF runs the HyCOM-coupled WRF."""
    def __init__(self,dstore,conf,section,wrf,ocstatus,keeprun=True,
                 wrfdiag_stream='auxhist1',hycominit=None,**kwargs):
        if not isinstance(hycominit,HYCOMInit):
            raise TypeError(
                'The hycominit argument to WRFCoupledHYCOM.__init__ must be a '
                'HYCOMInit object.  You passed a %s %s.'%
                (type(hycominit).__name__,repr(hycominit)))
        super(WRFCoupledHYCOM,self).__init__(dstore,conf,section,wrf,keeprun,
                                           wrfdiag_stream,**kwargs)
        self._hycominit=hycominit
        hycominiter=HYCOMIniter(self,ocstatus)

        self.couple('coupler','hafs_wm3c','wm3c_ranks',1)
        #self.couple('ocean','hycom','ocean_ranks',90,hycominiter)
        #self.couple('wrf','wrf','wrf_ranks')

	self._add_wave()

        # For backward compatibility, use ocean_ranks option as default:
        ocean_ranks=self.confint('ocean_ranks',150)
        self.couple('ocean','hycom','hycom_ranks',ocean_ranks,hycominiter)
        self.couplewrf()

    def read_hycom_init_vars(self):
        hsprod=self.hycominit.hycom_settings
        (av,loc)=(hsprod.available,hsprod.location)
        logger=self.log()
        if not loc:
            msg='%s: no location for hycom_settings'%(hsprod.did,)
            logger.error(msg)
            raise hwrf.exceptions.OceanInitFailed(msg)
        if not av:
            msg='%s: not available yet according to database'%(loc,)
            logger.error(msg)
            raise hwrf.exceptions.OceanInitFailed(msg)
        found=0
        with open(loc,'rt') as f:
            for line in f:
                m=re.match('export (\S+)=(\S+)\s*$',line)
                if m:
                    (var,val)=m.groups(0)
                    self.tvset(var,val)
                    logger.info('%s: set %s = %s'%(loc,var,repr(val)))
                    found+=1
                else:
                    logger.warning('%s: unrecognized line "%s"'%(loc,line))
        if found<1:
            msg='%s: no variables found in file'%(loc,)
            logger.error(msg)
            raise hwrf.exceptions.OceanInitFailed(msg)
        else:
            logger.info('%s: set %d vars'%(loc,found))
    @property
    def hycominit(self):
        """Returns the HYCOMInit object."""
        return self._hycominit

    def _add_wave(self):
        """!Internal function for adding wave coupling.  This must be
        implemented by a subclass.
        @protected"""
        pass

class HYCOMPost(hwrf.hwrftask.HWRFTask):

# todo:
# done - get vars from COM hycom_settings (or via recall_ocstatus)
# done - change volume3z to layers (hsk mods)
# done - modify infile so that don't need archv2data_2dv.in (similar to archv2data_2d.in)
# done - deliver coupled forecast products (archive files) to {com}
# done - deliver hycompost products to {com} (don't need outfile set to actual name) (odd names tho)
# - deliver runwrf/blkdat.input to {com}
# - raise exception on any errors (timeout, any others?)
# - add better logging information (on events)
# - add switches on which to create (volume_3z, volume_3d, surface_d (((surface_z))) )
# - also, make unpost python

    """Runs the ocean post-processor on the HyCOM output, in parallel
    with the model."""
    def __init__(self,ds,conf,section,fcstlen=126,**kwargs):
        super(HYCOMPost,self).__init__(ds,conf,section,**kwargs)
        self.fcstlen=fcstlen

    def run(self):
      """Called from the ocean post job to run the HyCOM post."""
      logger=self.log()
      self.state=RUNNING

      #produtil.fileop.chdir('WORKhwrf')
      with NamedDir(self.workdir,keep=not self.scrub, logger=logger,rm_first=True) as d:

        fcstlen=self.fcstlen
        FIXhycom=self.timestr('{FIXhycom}')
        PARMhycom=self.timestr('{PARMhycom}')
        opn=self.timestr('{out_prefix_nodate}')

        # get hycom subdomain specs from hycom_settings file
        hycomsettingsfile=self.timestr('{com}/{out_prefix}.hycom_settings')
        with open (hycomsettingsfile,'rt') as hsf:
          for line in hsf:
            m=re.match('^export idm=(.*)$',line)
            if m:
              idm=int(m.groups()[0])
            m=re.match('^export jdm=(.*)$',line)
            if m:
              jdm=int(m.groups()[0])
            m=re.match('^export kdm=(.*)$',line)
            if m:
              kdm=int(m.groups()[0])
            m=re.match('^export gridlabelout=(.*)$',line)
            if m:
               gridlabelout=m.groups()[0]
            m=re.match('^export RUNmodIDout=(.*)$',line)
            if m:
               RUNmodIDout=m.groups()[0]
        logger.info('POSTINFO- idm=%d jdm=%d kdm=%d gridlabelout=%s RUNmodIDout=%s '%(idm,jdm,kdm,gridlabelout,RUNmodIDout))

#        self.state=FAILED
#        raise

        ffrom='%s/hmon_%s.%s.regional.grid'%(FIXhycom,RUNmodIDout,gridlabelout)
        produtil.fileop.make_symlink(
            ffrom+'.a','regional.grid.a',force=True,logger=self.log())
        produtil.fileop.make_symlink(
            ffrom+'.b','regional.grid.b',force=True,logger=self.log())
        ffrom='%s/hmon_%s.%s.regional.depth'%(FIXhycom,RUNmodIDout,gridlabelout)
        produtil.fileop.make_symlink(
            ffrom+'.a','regional.depth.a',force=True,logger=self.log())
        produtil.fileop.make_symlink(
            ffrom+'.b','regional.depth.b',force=True,logger=self.log())

        stime=self.conf.cycle

        navtime=0   # if we can get last file from hycominit
        navtime=3 
        epsilon=.1
        while navtime<fcstlen+epsilon:
           archtime=to_datetime_rel(navtime*3600,stime)
           archtimestring=archtime.strftime('%Y_%j_%H')
           if archtime.hour==0 or archtime.hour==6 or archtime.hour==12 or archtime.hour==18:
              notabin='archv.%s'%(archtimestring)
           else:
              notabin='archs.%s'%(archtimestring)
           logfile=''.join([notabin,'.txt'])

           timesslept=0
           sleepmax=180
           while timesslept<sleepmax:
              if os.path.exists("../forecast/"+logfile):
                 break
              else:
                 timesslept=timesslept+1
                 logger.warning('Cannot find file %s %d times'%( repr(logfile),timesslept))
                 time.sleep(10)
           if timesslept>=sleepmax:
              logger.error('Cannot find file %s %d times - exiting'%( repr(logfile),timesslept))
              raise exception
        
           logger.info('Will create ocean products for %s '%( repr(notabin)))
           afile=''.join(['../forecast/'+notabin,'.a'])
           bfile=''.join(['../forecast/'+notabin,'.b'])
           produtil.fileop.make_symlink(afile,'archv.a',force=True,logger=logger)
           produtil.fileop.make_symlink(bfile,'archv.b',force=True,logger=logger)
           #####        create latlon gribs
#           with open ('for_latlon','wt') as fll:
#              fll.write(""" %d %d %d %d %d 0.0 0.0 120
#           """ %(int(stime.year),int(stime.month),int(stime.day),navtime,int(stime.hour)))
#           with open ('fort.22','wt') as f22:
#              f22.write(""" 1 1 %d %d
#           """ %(idm,jdm))
#           ofs_latlon=alias(exe(self.getexe('ofs_latlon')))
#           checkrun(ofs_latlon<'for_latlon',logger=logger)

           # create fort.22 and top of infile and top of inzfile
           with open ('fort.22','wt') as f22:
              f22.write(""" 120 """)
           with open('topinfile','wt') as inf:
                        inf.write("""archv.b
GRIB1
%d         'yyyy'   = year
%d         'month ' = month
%d         'day   ' = day
%d         'hour  ' = hour
%d         'verfhr' = verification hour
""" %( int(stime.year),int(stime.month),int(stime.day),int(stime.hour),navtime))

           with open('topzfile','wt') as inf:
                        inf.write("""archv.b
HYCOM
%d         'yyyy'   = year
%d         'month ' = month
%d         'day   ' = day
%d         'hour  ' = hour
%d         'verfhr' = verification hour
""" %( int(stime.year),int(stime.month),int(stime.day),int(stime.hour),navtime))

           if archtime.hour==0 or archtime.hour==6 or archtime.hour==12 or archtime.hour==18:
               logger.info('Do Volume HERE')
               ## volume_3z
               #concat topzinfile and parmfile
               parmfile='%s/hmon_rtofs.archv2data_3z.in'%(PARMhycom)
               filenames = ['topzfile',parmfile]
               with open('tempinfile', 'w') as iff:
                   for fname in filenames:
                       with open(fname) as filen:
                           iff.write(filen.read())
               #replace idm,jdm,kdm
               replacements={'&idm':repr(idm),'&jdm':repr(jdm),'&kdm':repr(kdm)}
               with open ('tempinfile') as inf:
                   with open ('infile','w') as outf:
                       for line in inf:
                          for src,targ in replacements.iteritems():
                             line=line.replace(src,targ)
                          outf.write(line)

               archv2data=alias(exe(self.getexe('hafs_archv2data3z')))
               checkrun(archv2data<'infile',logger=logger)
               i=1
               with open ('archv.b','rt') as ib:
                   with open ('partialb','wt') as pb:
                       for line in ib:
                           if i>4 and i<11:
                               pb.write(line)
                           i=i+1
               outfile='%s.%04d%02d%02d%02d.hmon_%s_3z.f%03d'%(opn,int(stime.year),int(stime.month),int(stime.day),int(stime.hour),RUNmodIDout,navtime)
               outfilea=outfile+'.a'
               outfileb=outfile+'.b'
               filenames = ['partialb', 'fort.51']
               with open(outfileb,'w') as ob:
                   for fname in filenames:
                       with open(fname) as filen:
                           ob.write(filen.read())
               os.rename('fort.051a',outfilea)
               deliver_file(outfilea,self.icstr('{com}/'+outfilea),keep=False,logger=logger)
               deliver_file(outfileb,self.icstr('{com}/'+outfileb),keep=False,logger=logger)
               remove_file('fort.51',info=True,logger=logger)

               ## volume_3d
               #concat topinfile and parmfile
               parmfile='%s/hmon_rtofs.archv2data_3d.in'%(PARMhycom)
               filenames = ['topinfile',parmfile]
               with open('tempinfile', 'w') as iff:
                   for fname in filenames:
                       with open(fname) as filen:
                           iff.write(filen.read())
               #replace idm,jdm,kdm
               replacements={'&idm':repr(idm),'&jdm':repr(jdm),'&kdm':repr(kdm)}
               with open ('tempinfile') as inf:
                   with open ('infile','w') as outf:
                       for line in inf:
                          for src,targ in replacements.iteritems():
                             line=line.replace(src,targ)
                          outf.write(line)

               archv2data=alias(exe(self.getexe('hafs_archv2data2d')))
               checkrun(archv2data<'infile',logger=logger)
               outfile='%s.t%02dz.F%03d.3d.grb'%(RUNmodIDout,int(stime.hour),navtime)
               gbfile=open(outfile,'wb')
               shutil.copyfileobj(open('fort.51','rb'),gbfile)
#               shutil.copyfileobj(open('ugrib','rb'),gbfile)
#               shutil.copyfileobj(open('vgrib','rb'),gbfile)
#               shutil.copyfileobj(open('pgrib','rb'),gbfile)
               gbfile.close()
#               deliver_file(outfile,self.icstr('{com}/'+outfile),keep=False,logger=logger)
               remove_file('fort.51',info=True,logger=logger)
               ## mld from 3d volume grib
               
               ## surface_d for volume
               parmfile='%s/hmon_rtofs.archv2data_2d.in'%(PARMhycom)
               filenames = ['topinfile',parmfile]
               with open('tempinfile', 'w') as iff:
                   for fname in filenames:
                       with open(fname) as filen:
                           iff.write(filen.read())
               #replace idm,jdm,kdm
               replacements={'&idm':repr(idm),'&jdm':repr(jdm),'&kdm':repr(kdm)}
               with open ('tempinfile') as inf:
                   with open ('infile','w') as outf:
                       for line in inf:
                          for src,targ in replacements.iteritems():
                             line=line.replace(src,targ)
                          outf.write(line)

               archv2data=alias(exe(self.getexe('hafs_archv2data2d')))
               checkrun(archv2data<'infile',logger=logger)
               outfile='%s.t%02dz.f%03d.3d.grb'%(RUNmodIDout,int(stime.hour),navtime)
               gbfile=open(outfile,'wb')
               shutil.copyfileobj(open('fort.51','rb'),gbfile)
#               shutil.copyfileobj(open('ugrib','rb'),gbfile)
#               shutil.copyfileobj(open('vgrib','rb'),gbfile)
#               shutil.copyfileobj(open('pgrib','rb'),gbfile)
               gbfile.close()
#               deliver_file(outfile,self.icstr('{com}/'+outfile),keep=False,logger=logger)
               remove_file('fort.51',info=True,logger=logger)

           else:
               logger.info('Do Surface HERE')
               ## surface_z  (not done in HyHWRF)

               ## surface_d
               parmfile='%s/hmon_rtofs.archv2data_2d.in'%(PARMhycom)
               filenames = ['topinfile',parmfile]
               with open('tempinfile', 'w') as iff:
                   for fname in filenames:
                       with open(fname) as filen:
                           iff.write(filen.read())
               #replace idm,jdm,kdm
               replacements={'&idm':repr(idm),'&jdm':repr(jdm),'&kdm':repr(1)}
               with open ('tempinfile') as inf:
                   with open ('infile','w') as outf:
                       for line in inf:
                          for src,targ in replacements.iteritems():
                             line=line.replace(src,targ)
                          outf.write(line)

               archv2data=alias(exe(self.getexe('hafs_archv2data2d')))
               checkrun(archv2data<'infile',logger=logger)
               outfile='%s.t%02dz.f%03d.3d.grb'%(RUNmodIDout,int(stime.hour),navtime)
               gbfile=open(outfile,'wb')
               shutil.copyfileobj(open('fort.51','rb'),gbfile)
#               shutil.copyfileobj(open('ugrib','rb'),gbfile)
#               shutil.copyfileobj(open('vgrib','rb'),gbfile)
#               shutil.copyfileobj(open('pgrib','rb'),gbfile)
               gbfile.close()
#               deliver_file(outfile,self.icstr('{com}/'+outfile),keep=False,logger=logger)
               remove_file('fort.51',info=True,logger=logger)
           
#        #####    deliver ab files to comout
           notabout='hmon_%s.%s'%(RUNmodIDout,archtimestring)
           deliver_file('../forecast/'+notabin+'.a',self.icstr('{com}/{out_prefix}.'+notabout+'.a',keep=True,logger=logger))
           deliver_file('../forecast/'+notabin+'.b',self.icstr('{com}/{out_prefix}.'+notabout+'.b',keep=True,logger=logger))

           # this is the frequency of files 
           navtime+=3
        logger.info('finishing up here')
        return -1

        #try:
        #    checkrun(scriptexe(self,'{USHhwrf}/hycom/ocean_post.sh'),
        #             logger=logger)
        #    self.state=COMPLETED
        #except Exception as e:
        #    self.state=FAILED
        #    logger.error("Ocean post failed: %s"%(str(e),),exc_info=True)
        #    raise
    def unrun(self):
        """Called from the unpost job to delete the HyCOM post output
        in preparation for a rerun of the entire post-processing for
        the cycle."""
        logger=self.log()
        self.state=RUNNING
#        try:
#            checkrun(scriptexe(self,'{USHhwrf}/hycom/ocean_unpost.sh'),
#                    logger=logger)
        self.state=COMPLETED
#        except Exception as e:
#            self.state=FAILED
#            logger.error("Ocean post failed: %s"%(str(e),),exc_info=True)
#            raise

