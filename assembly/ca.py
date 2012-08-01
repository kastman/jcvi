#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Prepare input files for Celera Assembler, dispatch based on file suffix::

*.fasta: convert-fasta-to-v2.pl
*.sff: sffToCA
*.fastq: fastqToCA
"""

import os.path as op
import sys
import logging

from glob import glob
from optparse import OptionParser
from collections import defaultdict

from Bio import SeqIO

from jcvi.formats.base import must_open
from jcvi.formats.fasta import Fasta, SeqRecord, \
    get_qual, iter_fasta_qual, write_fasta_qual
from jcvi.formats.blast import Blast
from jcvi.utils.range import range_minmax
from jcvi.utils.table import tabulate
from jcvi.apps.softlink import get_abs_path
from jcvi.apps.command import CAPATH
from jcvi.apps.base import ActionDispatcher, sh, set_grid, need_update, debug
debug()


def main():

    actions = (
        ('tracedb', 'convert trace archive files to frg file'),
        ('clr', 'prepare vector clear range file based on BLAST to vectors'),
        ('fasta', 'convert fasta to frg file'),
        ('sff', 'convert 454 reads to frg file'),
        ('fastq', 'convert Illumina reads to frg file'),
        ('shred', 'shred contigs into pseudo-reads'),
        ('astat', 'generate the coverage-rho scatter plot'),
        ('script', 'create gatekeeper script file to remove or add mates'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


frgTemplate = '''{{FRG
act:A
acc:{fragID}
rnd:1
sta:G
lib:{libID}
pla:0
loc:0
src:
.
seq:
{seq}
.
qlt:
{qvs}
.
hps:
.
clr:0,{slen}
}}'''

headerTemplate = '''{{VER
ver:2
}}
{{LIB
act:A
acc:{libID}
ori:U
mea:0.0
std:0.0
src:
.
nft:4
fea:
doTrim_initialQualityBased=0
doTrim_finalEvidenceBased=0
doRemoveSpurReads=0
doRemoveChimericReads=0
.
}}'''

DEFAULTQV = chr(ord('0') + 13)  # To pass initialTrim


def astat(args):
    """
    %prog astat coverage.log

    Create coverage-rho scatter plot.
    """
    p = OptionParser(astat.__doc__)
    p.add_option("--cutoff", default=1000, type="int",
                 help="Length cutoff [default: %default]")
    p.add_option("--genome", default="",
                 help="Genome name [default: %default]")
    p.add_option("--arrDist", default=False, action="store_true",
                 help="Use arrDist instead [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    covfile, = args
    cutoff = opts.cutoff
    genome = opts.genome
    plot_arrDist = opts.arrDist

    suffix = ".{0}".format(cutoff)
    small_covfile = covfile + suffix
    update_covfile = need_update(covfile, small_covfile)
    if update_covfile:
        fw = open(small_covfile, "w")
    else:
        logging.debug("Found `{0}`, will use this one".format(small_covfile))
        covfile = small_covfile

    fp = open(covfile)
    header = fp.next()
    if update_covfile:
        fw.write(header)

    data = []
    msg = "{0} tigs scanned ..."
    for row in fp:
        tigID, rho, covStat, arrDist = row.split()
        tigID = int(tigID)
        if tigID % 1000000 == 0:
            sys.stderr.write(msg.format(tigID) + "\r")

        rho, covStat, arrDist = [float(x) for x in (rho, covStat, arrDist)]
        if rho < cutoff:
            continue

        if update_covfile:
            fw.write(row)
        data.append((tigID, rho, covStat, arrDist))

    print >> sys.stderr, msg.format(tigID)

    from jcvi.graphics.base import plt

    logging.debug("Plotting {0} data points.".format(len(data)))
    tigID, rho, covStat, arrDist = zip(*data)

    y = arrDist if plot_arrDist else covStat
    ytag = "arrDist" if plot_arrDist else "covStat"

    fig = plt.figure(1, (7, 7))
    ax = fig.add_axes([.12, .1, .8, .8])
    ax.plot(rho, y, ".", color="lightslategrey")

    xtag = "rho"
    info = (genome, xtag, ytag)
    title = "{0} {1} vs. {2}".format(*info)
    ax.set_title(title)
    ax.set_xlabel(xtag)
    ax.set_ylabel(ytag)

    if plot_arrDist:
        ax.set_yscale('log')

    imagename = "{0}.png".format(".".join(info))
    plt.savefig(imagename, dpi=150)


def emitFragment(fw, fragID, libID, shredded_seq, fasta=False):
    """
    Print out the shredded sequence.
    """
    if fasta:
        s = SeqRecord(shredded_seq, id=fragID, description="")
        SeqIO.write([s], fw, "fasta")
        return

    seq = str(shredded_seq)
    slen = len(seq)
    qvs = DEFAULTQV * slen  # shredded reads have default low qv

    print >> fw, frgTemplate.format(fragID=fragID, libID=libID,
        seq=seq, qvs=qvs, slen=slen)


def shred(args):
    """
    %prog shred fastafile

    Similar to the method of `shredContig` in runCA script. The contigs are
    shredded into pseudo-reads with certain length and depth.
    """
    p = OptionParser(shred.__doc__)
    p.add_option("--depth", default=2, type="int",
            help="Desired depth of the reads [default: %default]")
    p.add_option("--readlen", default=1000, type="int",
            help="Desired length of the reads [default: %default]")
    p.add_option("--minctglen", default=150, type="int",
            help="Ignore contig sequence less than [default: %default]")
    p.add_option("--fasta", default=False, action="store_true",
            help="Output shredded reads as FASTA sequences [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    fastafile, = args
    libID = fastafile.split(".")[0]
    outfile = libID + ".depth{0}".format(opts.depth)
    if opts.fasta:
        outfile += ".fasta"
    else:
        outfile += ".frg"
    fw = must_open(outfile, "w", checkexists=True)
    f = Fasta(fastafile, lazy=True)

    if not opts.fasta:
       print >> fw, headerTemplate.format(libID=libID)

    """
    Taken from runCA:

                    |*********|
                    |###################|
    |--------------------------------------------------|
     ---------------1---------------
               ---------------2---------------
                         ---------------3---------------
    *** - center_increments
    ### - center_range_width
    """
    for ctgID, (name, rec) in enumerate(f.iteritems_ordered()):
        seq = rec.seq
        seqlen = len(seq)
        if seqlen < opts.minctglen:
            continue

        shredlen = min(seqlen - 50, opts.readlen)
        numreads = max(seqlen * opts.depth / shredlen, 1)
        center_range_width = seqlen - shredlen

        ranges = []
        if numreads == 1:
            ranges.append((0, shredlen))
        else:
            prev_begin = -1
            center_increments = center_range_width * 1. / (numreads - 1)
            for i in xrange(numreads):
                begin = center_increments * i
                end = begin + shredlen
                begin, end = int(begin), int(end)

                if begin == prev_begin:
                    continue

                ranges.append((begin, end))
                prev_begin = begin

        for shredID, (begin, end) in enumerate(ranges):
            shredded_seq = seq[begin:end]
            fragID = "{0}.{1}.frag{2}.{3}-{4}".format(libID, ctgID, shredID, begin, end)
            emitFragment(fw, fragID, libID, shredded_seq, fasta=opts.fasta)

    fw.close()
    logging.debug("Shredded reads are written to `{0}`.".format(outfile))


def script(args):
    """
    %prog script bfs_rfs libs

    `bfs_rfs` contains the joined results from Brian's `classifyMates`. We want
    to keep the RFS result (but not in the BFS result) to retain actual MP. Libs
    contain a list of lib iids, use comma to separate, e.g. "9,10,11".
    """
    p = OptionParser(script.__doc__)

    opts, args = p.parse_args(args)
    if len(args) != 2:
        sys.exit(p.print_help())

    fsfile, libs = args
    libs = [int(x) for x in libs.split(",")]
    fp = open(fsfile)
    not_found = ("limited", "exhausted")
    counts = defaultdict(int)
    pe, mp = 0, 0
    both, noidea = 0, 0
    total = 0

    for i in libs:
        print "lib iid {0} allfragsunmated 1".format(i)

    for row in fp:
        frgiid, bfs, rfs = row.split()
        bfs = (bfs not in not_found)
        rfs = (rfs not in not_found)
        if bfs and (not rfs):
            pe += 1
        if rfs and (not bfs):
            mp += 1
            frgiid = int(frgiid)
            mateiid = frgiid + 1
            print "frg iid {0} mateiid {1}".format(frgiid, mateiid)
            print "frg iid {0} mateiid {1}".format(mateiid, frgiid)
        if bfs and rfs:
            both += 1
        if (not bfs) and (not rfs):
            noidea += 1
        total += 1

    assert pe + mp + both + noidea == total
    counts[("PE", "N")] = pe
    counts[("MP", "N")] = mp
    counts[("Both", "N")] = both
    counts[("No Idea", "N")] = noidea

    table = tabulate(counts)
    func = lambda a: a * 100. / total
    table = table.withNewColumn("Percentage", callback=func,
            columns=("N",), digits=2)
    print >> sys.stderr, table


def tracedb(args):
    """
    %prog tracedb <xml|lib|frg>

    Run `tracedb-to-frg.pl` within current folder.
    """
    p = OptionParser(tracedb.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    action, = args
    assert action in ("xml", "lib", "frg")

    CMD = CAPATH("tracedb-to-frg.pl")
    xmls = glob("xml*")

    if action == "xml":
        for xml in xmls:
            cmd = CMD + " -xml {0}".format(xml)
            sh(cmd, outfile="/dev/null", errfile="/dev/null", background=True)

    elif action == "lib":
        cmd = CMD + " -lib {0}".format(" ".join(xmls))
        sh(cmd)

    elif action == "frg":
        for xml in xmls:
            cmd = CMD + " -frg {0}".format(xml)
            sh(cmd, background=True)


def make_qual(fastafile, defaultqual=21):
    """
    Make a qualfile with default qual value if not available
    """
    assert op.exists(fastafile)

    qualfile = get_qual(fastafile)
    if qualfile is None:
        qualfile = get_qual(fastafile, check=False)
        qualhandle = open(qualfile, "w")

        for rec in iter_fasta_qual(fastafile, None, defaultqual=defaultqual):
            write_fasta_qual(rec, None, qualhandle)

        logging.debug("write qual values to file `{0}`".format(qualfile))
        qualhandle.close()

    return qualfile


def make_matepairs(fastafile):
    """
    Assumes the mates are adjacent sequence records
    """
    assert op.exists(fastafile)

    matefile = fastafile.rsplit(".", 1)[0] + ".mates"
    if op.exists(matefile):
        logging.debug("matepairs file `{0}` found".format(matefile))
    else:
        logging.debug("parsing matepairs from `{0}`".format(fastafile))
        matefw = open(matefile, "w")
        it = SeqIO.parse(fastafile, "fasta")
        for fwd, rev in zip(it, it):
            print >> matefw, "{0}\t{1}".format(fwd.id, rev.id)

        matefw.close()

    return matefile


def add_size_option(p):
    p.add_option("-s", dest="size", default=0, type="int",
            help="insert has mean size of [default: %default] " + \
                 "stddev is assumed to be 20% around mean size")


get_mean_sv = lambda size: (size, size / 5)


def fasta(args):
    """
    %prog fasta fastafile

    Convert reads formatted as FASTA file, and convert to CA frg file. If .qual
    file is found, then use it, otherwise just make a fake qual file. Mates are
    assumed as adjacent sequence records (i.e. /1, /2, /1, /2 ...) unless a
    matefile is given.
    """
    p = OptionParser(fasta.__doc__)
    p.add_option("-m", dest="matefile", default=None,
            help="matepairs file")
    set_grid(p)
    add_size_option(p)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    grid = opts.grid

    fastafile, = args
    plate = op.basename(fastafile).split(".")[0]

    mated = (opts.size != 0)
    mean, sv = get_mean_sv(opts.size)

    if mated:
        libname = "Sanger{0}Kb-".format(opts.size / 1000) + plate
    else:
        libname = "SangerFrags-" + plate

    frgfile = libname + ".frg"

    qualfile = make_qual(fastafile)
    if mated:
        if opts.matefile:
            matefile = opts.matefile
            assert op.exists(matefile)
        else:
            matefile = make_matepairs(fastafile)

    cmd = CAPATH("convert-fasta-to-v2.pl")
    cmd += " -l {0} -s {1} -q {2} ".\
            format(libname, fastafile, qualfile)
    if mated:
        cmd += "-mean {0} -stddev {1} -m {2} ".format(mean, sv, matefile)

    sh(cmd, grid=grid, outfile=frgfile)


def sff(args):
    """
    %prog sff sffiles

    Convert reads formatted as 454 SFF file, and convert to CA frg file.
    Turn --nodedup on if another deduplication mechanism is used (e.g.
    CD-HIT-454). See assembly.sff.deduplicate().
    """
    p = OptionParser(sff.__doc__)
    p.add_option("--prefix", dest="prefix", default=None,
            help="Output frg filename prefix")
    p.add_option("--nodedup", default=False, action="store_true",
            help="Do not remove duplicates [default: %default]")
    set_grid(p)
    add_size_option(p)

    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(p.print_help())

    grid = opts.grid

    sffiles = args
    plates = [x.split(".")[0].split("_")[-1] for x in sffiles]

    mated = (opts.size != 0)
    mean, sv = get_mean_sv(opts.size)

    if len(plates) > 1:
        plate = plates[0][:-1] + 'X'
    else:
        plate = "_".join(plates)

    if mated:
        libname = "Titan{0}Kb-".format(opts.size / 1000) + plate
    else:
        libname = "TitanFrags-" + plate

    if opts.prefix:
        libname = opts.prefix

    cmd = CAPATH("sffToCA")
    cmd += " -libraryname {0} -output {0} ".format(libname)
    cmd += " -clear 454 -trim chop "
    if mated:
        cmd += " -linker titanium -insertsize {0} {1} ".format(mean, sv)
    if opts.nodedup:
        cmd += " -nodedup "

    cmd += " ".join(sffiles)

    sh(cmd, grid=grid)


def fastq(args):
    """
    %prog fastq fastqfile

    Convert reads formatted as FASTQ file, and convert to CA frg file.
    """
    from jcvi.formats.fastq import guessoffset

    p = OptionParser(fastq.__doc__)
    phdchoices = ("33", "64")
    p.add_option("--outtie", dest="outtie", default=False, action="store_true",
            help="Are these outie reads? [default: %default]")
    p.add_option("--phred", default=None, choices=phdchoices,
            help="Phred score offset {0} [default: guess]".format(phdchoices))
    add_size_option(p)

    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(p.print_help())

    fastqfiles = [get_abs_path(x) for x in args]

    mated = (opts.size != 0)
    outtie = opts.outtie
    libname = op.basename(args[0]).split(".")[0]
    libname = libname.replace("_1_sequence", "")

    frgfile = libname + ".frg"
    mean, sv = get_mean_sv(opts.size)

    cmd = CAPATH("fastqToCA")
    cmd += " -libraryname {0} ".format(libname)
    fastqs = " ".join("-reads {0}".format(x) for x in fastqfiles)
    if mated:
        assert len(args) in (1, 2), "you need one or two fastq files for mated library"
        fastqs = "-mates {0}".format(",".join(fastqfiles))
        cmd += "-insertsize {0} {1} ".format(mean, sv)
    cmd += fastqs

    offset = int(opts.phred) if opts.phred else guessoffset([fastqfiles[0]])
    illumina = (offset == 64)
    if illumina:
        cmd += " -type illumina"
    if outtie:
        cmd += " -outtie"

    sh(cmd, outfile=frgfile)


def clr(args):
    """
    %prog blastfile fastafiles

    Calculate the vector clear range file based BLAST to the vectors.
    """
    p = OptionParser(clr.__doc__)
    opts, args = p.parse_args(args)

    if len(args) < 2:
        sys.exit(not p.print_help())

    blastfile = args[0]
    fastafiles = args[1:]

    sizes = {}
    for fa in fastafiles:
        f = Fasta(fa)
        sizes.update(f.itersizes())

    b = Blast(blastfile)
    seen = set()
    for query, hits in b.iter_hits():

        qsize = sizes[query]
        vectors = list((x.qstart, x.qstop) for x in hits)
        vmin, vmax = range_minmax(vectors)

        left_size = vmin - 1
        right_size = qsize - vmax

        if left_size > right_size:
            clr_start, clr_end = 0, vmin
        else:
            clr_start, clr_end = vmax, qsize

        print "\t".join(str(x) for x in (query, clr_start, clr_end))
        del sizes[query]

    for q, size in sorted(sizes.items()):
        print "\t".join(str(x) for x in (q, 0, size))


if __name__ == '__main__':
    main()
