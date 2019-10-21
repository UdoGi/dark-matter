from __future__ import print_function

import os
import re
import sqlite3
import sys
import numpy as np
from Bio import SeqIO
from cachetools import LRUCache, cachedmethod
from collections import defaultdict
from json import load
from operator import attrgetter, itemgetter
from os.path import dirname, exists, join
from six import string_types
from six.moves.urllib.parse import quote
from textwrap import fill
from warnings import warn

from dark.dimension import dimensionalIterator
from dark.errors import DatabaseDuplicationError
from dark.fasta import FastaReads
from dark.fastq import FastqReads
from dark.filter import TitleFilter
from dark.genbank import GenomeRanges
from dark.html import NCBISequenceLinkURL, NCBISequenceLink
from dark.reads import Reads
from dark.taxonomy import (
    isRNAVirus, formatLineage, lineageTaxonomyLinks, Hierarchy)


class PathogenSampleFiles(object):
    """
    Maintain a cache of FASTA/FASTQ file names for the samples that contain a
    given pathogen, create de-duplicated (by read id) FASTA/FASTQ files
    for each pathogen/sample pair, provide functions to write out index files
    of samples numbers (which are generated here in C{self.add}),
    and provide a filename lookup function for pathogen/sample combinations
    or just pathogen accessions by themselves.

    @param proteinGrouper: An instance of C{ProteinGrouper}.
    @param format_: A C{str}, either 'fasta' or 'fastq' indicating the format
        of the files containing the reads matching proteins.
    @raise ValueError: If C{format_} is unknown.
    """
    def __init__(self, proteinGrouper, format_='fasta'):
        self._proteinGrouper = proteinGrouper
        if format_ in ('fasta', 'fastq'):
            self._format = format_
            self._readsClass = FastaReads if format_ == 'fasta' else FastqReads
        else:
            raise ValueError("format_ must be either 'fasta' or 'fastq'.")
        self._pathogens = {}
        self._samples = {}
        self._readsFilenames = {}

    def add(self, genomeAccession, sampleName):
        """
        Add a (pathogen accession number, sample name) combination and get its
        FASTA/FASTQ file name and unique read count. Write the FASTA/FASTQ file
        if it does not already exist. Save the unique read count into
        C{self._proteinGrouper}.

        @param genomeAccession: A C{str} pathogen accession number.
        @param sampleName: A C{str} sample name.
        @return: A C{str} giving the FASTA/FASTQ file name holding all the
            reads (without duplicates, by id) from the sample that matched the
            proteins in the given pathogen.
        """
        sampleIndex = self._samples.setdefault(sampleName, len(self._samples))

        try:
            return self._readsFilenames[(genomeAccession, sampleIndex)]
        except KeyError:
            reads = Reads()
            for proteinMatch in self._proteinGrouper.genomeAccessions[
                    genomeAccession][sampleName]['proteins'].values():
                for read in self._readsClass(proteinMatch['readsFilename']):
                    reads.add(read)
            saveFilename = join(
                proteinMatch['outDir'],
                'pathogen-%s-sample-%d.%s' % (genomeAccession, sampleIndex,
                                              self._format))
            reads.filter(removeDuplicatesById=True)
            nReads = reads.save(saveFilename, format_=self._format)
            # Save the unique read count into self._proteinGrouper
            self._proteinGrouper.genomeAccessions[
                genomeAccession][sampleName]['uniqueReadCount'] = nReads
            self._readsFilenames[
                (genomeAccession, sampleIndex)] = saveFilename
            return saveFilename

    def lookup(self, genomeAccession, sampleName):
        """
        Look up a pathogen accession number, sample name combination and get
        its FASTA/FASTQ file name.

        This method should be used instead of C{add} in situations where
        you want an exception to be raised if a pathogen/sample combination has
        not already been passed to C{add}.

        @param genomeAccession: A C{str} pathogen accession number.
        @param sampleName: A C{str} sample name.
        @raise KeyError: If the pathogen accession number or sample name have
            not been seen, either individually or in combination.
        @return: A C{str} filename retrieved from self._readsFilenames
        """
        return self._readsFilenames[
            (genomeAccession, self._samples[sampleName])]

    def writeSampleIndex(self, fp):
        """
        Write a file of sample indices and names, sorted by index.

        @param fp: A file-like object, opened for writing.
        """
        print('\n'.join(
            '%d %s' % (index, name) for (index, name) in
            sorted((index, name) for (name, index) in self._samples.items())
        ), file=fp)


class ProteinGrouper(object):
    """
    Group matched proteins by the pathogen they come from.

    @param proteinGenomeDatabase: A connection to an Sqlite3 database
        holding protein and genome information, as built by
        C{make-protein-database.py}.
    @param assetDir: The C{str} directory name where
        C{noninteractive-alignment-panel.py} put its HTML, blue plot and
        alignment panel images, and FASTA or FASTQ files. This must be relative
        to the filenames that will later be passed to C{addFile}.
    @param sampleName: A C{str} sample name. This takes precedence over
        C{sampleNameRegex} (the two cannot be used together, obviously).
    @param sampleNameRegex: A C{str} regular expression that can be used to
        extract a short sample name from full file names subsequently passed
        to C{self.addFile}. The regular expression must have a matching group
        (delimited by parentheses) to capture the part of the file name that
        should be used as the sample name.
    @param format_: A C{str}, either 'fasta' or 'fastq' indicating the format
        of the files containing the reads matching proteins.
    @param saveReadLengths: If C{True}, save the lengths of all reads matching
        proteins.
    @param titleRegex: A regex that pathogen names must match.
        Note that this matching is done on the final part of the protein title
        in square brackets, according to the convention used by the NCBI viral
        refseq database and RVDB.
    @param negativeTitleRegex: A regex that pathogen names must not match.
        Note that this matching is done on the final part of the protein title
        in square brackets, according to the convention used by the NCBI viral
        refseq database and RVDB.
    @param pathogenDataDir: The C{str} directory where per-pathogen information
        (e.g., collected reads across all samples) should be written. Will be
        created (in C{self.toHTML}) if it doesn't exist.
    @raise ValueError: If C{format_} is unknown.
    """

    VIRALZONE = 'https://viralzone.expasy.org/search?query='
    ICTV = 'https://talk.ictvonline.org/search-124283882/?q='
    READCOUNT_MARKER = '*READ-COUNT*'
    READ_AND_HSP_COUNT_STR_SEP = '/'

    def __init__(self, proteinGenomeDatabase, taxonomyDatabase, assetDir='out',
                 sampleName=None, sampleNameRegex=None, format_='fasta',
                 saveReadLengths=False, titleRegex=None,
                 negativeTitleRegex=None, pathogenDataDir='pathogen-data'):
        self._db = proteinGenomeDatabase
        self._taxdb = taxonomyDatabase
        self._assetDir = assetDir
        self._sampleName = sampleName
        self._sampleNameRegex = (re.compile(sampleNameRegex) if sampleNameRegex
                                 else None)
        if format_ in ('fasta', 'fastq'):
            self._format = format_
        else:
            raise ValueError("format_ must be either 'fasta' or 'fastq'.")
        self._saveReadLengths = saveReadLengths

        if titleRegex or negativeTitleRegex:
            self.titleFilter = TitleFilter(
                positiveRegex=titleRegex, negativeRegex=negativeTitleRegex)
        else:
            self.titleFilter = None

        self._pathogenDataDir = pathogenDataDir

        # genomeAccessions will be a dict of dicts of dicts. The first
        # two keys will be a pathogen accession and a sample name. The
        # final dict will contain 'proteins' (a list of dicts) and
        # 'uniqueReadCount' (an int).
        self.genomeAccessions = defaultdict(dict)
        # sampleNames is keyed by sample name and will have values that hold
        # the sample's alignment panel index.html file.
        self.sampleNames = {}
        self.pathogenSampleFiles = PathogenSampleFiles(self, format_=format_)

    def _title(self, pathogenType):
        """
        Create a title summarizing the pathogens and samples.

        @param pathogenType: A C{str}, either 'viral' or 'bacterial'.
        @return: A C{str} title.
        """

        assert pathogenType in ('viral', 'bacterial')

        nPathogens = len(self.genomeAccessions)
        nSamples = len(self.sampleNames)

        if pathogenType == 'bacterial':
            what = 'bacterium' if nPathogens == 1 else 'bacteria'
        else:
            what = 'virus%s' % ('' if nPathogens == 1 else 'es')

        return (
            'Overall, proteins from %d %s were found in %d sample%s.' %
            (nPathogens, what, nSamples, '' if nSamples == 1 else 's'))

    def addFile(self, filename, fp):
        """
        Read and record protein information for a sample.

        @param filename: A C{str} file name.
        @param fp: An open file pointer to read the file's data from.
        @raise ValueError: If information for a pathogen/protein/sample
            combination is given more than once.
        """
        if self._sampleName:
            sampleName = self._sampleName
        elif self._sampleNameRegex:
            match = self._sampleNameRegex.search(filename)
            if match:
                sampleName = match.group(1)
            else:
                sampleName = filename
        else:
            sampleName = filename

        outDir = join(dirname(filename), self._assetDir)

        self.sampleNames[sampleName] = join(outDir, 'index.html')

        for index, proteinLine in enumerate(fp):
            (coverage, medianScore, bestScore, readCount, hspCount,
             proteinLength, longName) = proteinLine.split(None, 6)

            proteinInfo = self._db.findProtein(longName)
            if proteinInfo is None:
                raise ValueError('Could not find protein info for %r.' %
                                 longName)
            proteinName = (proteinInfo['product'] or proteinInfo['gene'] or
                           'unknown')
            proteinAccession = proteinInfo['accession']

            genomeInfo = self._db.findGenome(longName)
            genomeName = genomeInfo['name']
            genomeAccession = genomeInfo['accession']

            # Ignore genomes with names we don't want.
            if (self.titleFilter and self.titleFilter.accept(
                    genomeName) == TitleFilter.REJECT):
                continue

            if sampleName not in self.genomeAccessions[genomeAccession]:
                self.genomeAccessions[genomeAccession][sampleName] = {
                    'proteins': {},
                    'uniqueReadCount': None,
                }

            proteins = self.genomeAccessions[
                genomeAccession][sampleName]['proteins']

            # We should only receive one line of information for a given
            # genome/sample/protein combination.
            if proteinAccession in proteins:
                raise ValueError(
                    'Protein %r already seen for genome %r (%s) sample %r.' %
                    (proteinAccession, genomeName, genomeAccession,
                     sampleName))

            readsFilename = join(outDir,
                                 '%s.%s' % (proteinAccession, self._format))

            if longName.startswith(SqliteIndexWriter.SEQUENCE_ID_PREFIX +
                                   SqliteIndexWriter.SEQUENCE_ID_SEPARATOR):
                proteinURL = NCBISequenceLinkURL(longName, field=2)
                genomeURL = NCBISequenceLinkURL(longName, field=4)
            else:
                proteinURL = genomeURL = None

            proteinInfo = proteins[proteinAccession] = {
                'accession': proteinAccession,
                'bestScore': float(bestScore),
                'bluePlotFilename': join(outDir, '%s.png' % proteinAccession),
                'coverage': float(coverage),
                'readsFilename': readsFilename,
                'hspCount': int(hspCount),
                'index': index,
                'medianScore': float(medianScore),
                'outDir': outDir,
                'proteinLength': int(proteinLength),
                'proteinName': proteinName,
                'proteinURL': proteinURL,
                'genomeURL': genomeURL,
                'readCount': int(readCount),
            }

            if proteinInfo['readCount'] == proteinInfo['hspCount']:
                proteinInfo['readAndHspCountStr'] = readCount
            else:
                proteinInfo['readAndHspCountStr'] = '%s%s%s' % (
                    readCount, self.READ_AND_HSP_COUNT_STR_SEP, hspCount)

            if self._saveReadLengths:
                readsClass = (FastaReads if self._format == 'fasta'
                              else FastqReads)
                proteins[proteinName]['readLengths'] = tuple(
                    len(read) for read in readsClass(readsFilename))

    def _computeUniqueReadCounts(self):
        """
        Add all pathogen / sample combinations to self.pathogenSampleFiles.

        This will make all de-duplicated (by id) FASTA/FASTQ files and store
        the number of de-duplicated reads into C{self.genomeAccessions}.
        """
        for genomeAccession, samples in self.genomeAccessions.items():
            for sampleName in samples:
                self.pathogenSampleFiles.add(genomeAccession, sampleName)

    def toStr(self, title=None, preamble=None, pathogenType='viral'):
        """
        Produce a string representation of the pathogen summary.

        @param title: The C{str} title for the output.
        @param preamble: The C{str} descriptive preamble, or C{None} if no
            preamble is needed.
        @param pathogenType: A C{str}, either 'viral' or 'bacterial'.

        @return: A C{str} suitable for printing.
        """
        # Note that the string representation contains much less
        # information than the HTML summary. E.g., it does not contain the
        # unique (de-duplicated, by id) read count, since that is only computed
        # when we are making combined FASTA files of reads matching a
        # pathogen.

        assert pathogenType in ('viral', 'bacterial')

        title = title or 'Summary of %s.' % (
            'bacteria' if pathogenType == 'bacterial' else 'viruses')

        readCountGetter = itemgetter('readCount')
        result = []
        append = result.append

        result.extend((title, ''))
        if preamble:
            result.extend((preamble, ''))
        result.extend((self._title(pathogenType), ''))

        for genomeAccession, samples in self.genomeAccessions.items():
            genomeInfo = self._db.findGenome(genomeAccession)
            genomeName = genomeInfo['name']
            sampleCount = len(samples)
            append('%s (in %d sample%s)' %
                   (genomeName,
                    sampleCount, '' if sampleCount == 1 else 's'))
            for sampleName in sorted(samples):
                proteins = samples[sampleName]['proteins']
                proteinCount = len(proteins)
                totalReads = sum(readCountGetter(p) for p in proteins.values())
                append('  %s (%d protein%s, %d read%s)' %
                       (sampleName,
                        proteinCount, '' if proteinCount == 1 else 's',
                        totalReads, '' if totalReads == 1 else 's'))
                for proteinName in sorted(proteins):
                    append(
                        '    %(coverage).2f\t%(medianScore).2f\t'
                        '%(bestScore).2f\t%(readAndHspCountStr)3s\t'
                        '%(proteinName)s'
                        % proteins[proteinName])
            append('')

        return '\n'.join(result)

    def _genomeName(self, genomeAccession):
        """
        Get the name of a genome, given its accession number.

        @param genomeAccession: A C{str} pathogen accession number.
        @return: A C{str} genome name.
        """
        return self._db.findGenome(genomeAccession)['organism']

    def toHTML(self, pathogenPanelFilename=None, minProteinFraction=0.0,
               pathogenType='viral', title='Summary of pathogens',
               preamble=None, sampleIndexFilename=None,
               omitVirusLinks=False):
        """
        Produce an HTML string representation of the pathogen summary.

        @param pathogenPanelFilename: If not C{None}, a C{str} filename to
            write a pathogen panel PNG image to.
        @param minProteinFraction: The C{float} minimum fraction of proteins
            in a pathogen that must be matched by a sample in order for that
            pathogen to be displayed for that sample.
        @param pathogenType: A C{str} giving the type of the pathogen involved,
            either 'bacterial' or 'viral'.
        @param title: The C{str} title for the HTML page.
        @param preamble: The C{str} descriptive preamble for the HTML page, or
            C{None} if no preamble is needed.
        @param sampleIndexFilename: A C{str} filename to write a sample index
            file to. Lines in the file will have an integer index, a space, and
            then the sample name.
        @param omitVirusLinks: If C{True}, links to ICTV and ViralZone will be
            omitted in output.
        @return: An HTML C{str} suitable for printing.
        """
        if pathogenType not in ('bacterial', 'viral'):
            raise ValueError(
                "Unrecognized pathogenType argument: %r. Value must be either "
                "'bacterial' or 'viral'." % pathogenType)

        if not exists(self._pathogenDataDir):
            os.mkdir(self._pathogenDataDir)

        self._computeUniqueReadCounts()

        if sampleIndexFilename:
            with open(sampleIndexFilename, 'w') as fp:
                self.pathogenSampleFiles.writeSampleIndex(fp)

        # Figure out if we have to delete some pathogens because the
        # fraction of their proteins that we have matches for is too low.
        if minProteinFraction > 0.0:
            toDelete = defaultdict(list)
            for genomeAccession in self.genomeAccessions:
                genomeInfo = self._db.findGenome(genomeAccession)
                proteinCount = genomeInfo['proteinCount']
                assert proteinCount > 0
                for s in self.genomeAccessions[genomeAccession]:
                    sampleProteinFraction = (
                        len(self.genomeAccessions[
                            genomeAccession][s]['proteins']) /
                        proteinCount)
                    if sampleProteinFraction < minProteinFraction:
                        toDelete[genomeAccession].append(s)

            for genomeAccession, samples in toDelete.items():
                for sample in samples:
                    del self.genomeAccessions[genomeAccession][sample]

        genomeAccessions = sorted(
            (genomeAccession for genomeAccession in self.genomeAccessions
             if len(self.genomeAccessions[genomeAccession]) > 0),
            key=self._genomeName)
        nPathogenNames = len(genomeAccessions)
        sampleNames = sorted(self.sampleNames)

        # Be careful with commas in the following! Long lines that should
        # be continued unbroken do not end with a comma.
        result = [
            '<html>',
            '<head>',
            '<title>',
            title,
            '</title>',
            '<meta charset="UTF-8">',

            '<link rel="stylesheet"',
            'href="https://stackpath.bootstrapcdn.com/bootstrap/'
            '3.4.1/css/bootstrap.min.css"',
            'integrity="sha384-HSMxcRTRxnN+Bdg0JdbxYKrThecOKuH5z'
            'CYotlSAcp1+c8xmyTe9GYg1l9a69psu"',
            'crossorigin="anonymous">',

            '<link rel="stylesheet" href="bootstrap-treeview.min.css">',

            '</head>',
            '<body>',
            '<script',
            'src="https://code.jquery.com/jquery-3.4.1.min.js"',
            'integrity="sha256-CSXorXvZcTkaix6Yvo6HppcZGetbYMGWSFlBw8HfCJo="',
            'crossorigin="anonymous"></script>',

            '<script',
            'src="https://stackpath.bootstrapcdn.com/bootstrap/'
            '3.4.1/js/bootstrap.min.js"',
            'integrity="sha384-aJ21OjlMXNL5UyIl/XNwTMqvzeRMZH2w8c5cRVpzpU8Y5b'
            'ApTppSuUkhZXN0VxHd"',
            'crossorigin="anonymous"></script>',

            '<script src="bootstrap-treeview.min.js"></script>',

            '<style>',
            '''\
            body {
                margin-left: 2%;
                margin-right: 2%;
            }
            hr {
                display: block;
                margin-top: 0.5em;
                margin-bottom: 0.5em;
                margin-left: auto;
                margin-right: auto;
                border-style: inset;
                border-width: 1px;
            }
            p.pathogen {
                margin-top: 10px;
                margin-bottom: 3px;
            }
            p.sample {
                margin-top: 10px;
                margin-bottom: 3px;
            }
            .sample {
                margin-top: 5px;
                margin-bottom: 2px;
            }
            ul {
                margin-bottom: 2px;
            }
            .indented {
                margin-left: 2em;
            }
            .sample-name {
                font-size: 125%;
                font-weight: bold;
            }
            .pathogen-name {
                font-size: 125%;
                font-weight: bold;
            }
            .index-name {
                font-weight: bold;
            }
            .index {
                font-size: small;
            }
            .host {
                font-size: small;
            }
            .taxonomy {
                font-size: small;
            }
            .protein-name {
            }
            .stats {
                font-family: "Courier New", Courier, monospace;
                white-space: pre;
            }
            .protein-list {
                margin-top: 2px;
            }''',
            '</style>',
            '</head>',
            '<body>',
        ]

        proteinFieldsDescription = [
            '<p>',
            'In all bullet point protein lists below, there are the following '
            'numeric fields:',
            '<ol>',
            '<li>Coverage fraction.</li>',
            '<li>Median bit score.</li>',
            '<li>Best bit score.</li>',
            '<li>Read count (if the HSP count differs, read and HSP ',
            ('counts are both given, separated by "%s").</li>' %
             self.READ_AND_HSP_COUNT_STR_SEP),
        ]

        if self._saveReadLengths:
            proteinFieldsDescription.append(
                '<li>All read lengths (in parentheses).</li>')

        proteinFieldsDescription.extend([
            '<li>Protein name.</li>',
            '</ol>',
            '</p>',
        ])

        append = result.append

        append('<h1>%s</h1>' % title)
        if preamble:
            append('<p>%s</p>' % preamble)
        append('<p>')
        append(self._title(pathogenType))

        # Emit a div to hold the taxonomy tree.
        append('<div id="tree"></div>')

        if minProteinFraction > 0.0:
            percent = minProteinFraction * 100.0
            if nPathogenNames < len(self.genomeAccessions):
                if nPathogenNames == 1:
                    append('Pathogen protein fraction filtering has been '
                           'applied, so information on only 1 pathogen is '
                           'displayed. This is the only pathogen for which at '
                           'least one sample matches at least %.2f%% of the '
                           'pathogen proteins.' % percent)
                else:
                    append('Pathogen protein fraction filtering has been '
                           'applied, so information on only %d pathogens is '
                           'displayed. These are the only pathogens for which '
                           'at least one sample matches at least %.2f%% of '
                           'the pathogen proteins.' % (nPathogenNames,
                                                       percent))
            else:
                append('Pathogen protein fraction filtering was applied, '
                       'but all pathogens have at least %.2f%% of their '
                       'proteins matched by at least one sample.' % percent)

        append('</p>')

        if pathogenPanelFilename and genomeAccessions:
            self.pathogenPanel(pathogenPanelFilename)
            append('<p>')
            append('<a href="%s">Panel showing read count per pathogen, '
                   'per sample.</a>' % pathogenPanelFilename)
            append('Red vertical bars indicate samples with an unusually '
                   'high read count.')
            append('</p>')

        result.extend(proteinFieldsDescription)

        # Write a linked table of contents by pathogen.
        append('<p><span class="index-name">Pathogen index:</span>')
        append('<span class="index">')
        for genomeAccession in genomeAccessions:
            genomeInfo = self._db.findGenome(genomeAccession)
            append('<a href="#pathogen-%s">%s</a>' % (genomeAccession,
                                                      genomeInfo['organism']))
            append('&middot;')
        # Get rid of final middle dot and add a period.
        result.pop()
        result[-1] += '.'
        append('</span></p>')

        # Write a linked table of contents by sample.
        append('<p><span class="index-name">Sample index:</span>')
        append('<span class="index">')
        for sampleName in sampleNames:
            append('<a href="#sample-%s">%s</a>' % (sampleName, sampleName))
            append('&middot;')
        # Get rid of final middle dot and add a period.
        result.pop()
        result[-1] += '.'
        append('</span></p>')

        # Write all pathogens (with samples (with proteins)).
        append('<hr>')
        append('<h1>Pathogens by sample</h1>')

        taxonomyHierarchy = Hierarchy()

        for genomeAccession in genomeAccessions:
            samples = self.genomeAccessions[genomeAccession]
            sampleCount = len(samples)
            genomeInfo = self._db.findGenome(genomeAccession)
            pathogenProteinCount = genomeInfo['proteinCount']

            lineage = self._taxdb.lineage(genomeInfo['taxonomyId'])

            if lineage:
                taxonomyHierarchy.add(lineage, genomeAccession)
                lineageHTML = ', '.join(lineageTaxonomyLinks(lineage))
            else:
                lineageHTML = ''

            pathogenLinksHTML = ' %s, %s' % (
                genomeInfo['databaseName'],
                NCBISequenceLink(genomeAccession))

            if pathogenType == 'viral' and not omitVirusLinks:
                quoted = quote(genomeInfo['organism'])
                pathogenLinksHTML += (
                    ', <a href="%s%s">ICTV</a>, <a href="%s%s">ViralZone</a>.'
                ) % (self.ICTV, quoted, self.VIRALZONE, quoted)
            else:
                pathogenLinksHTML += '.'

            proteinCountStr = (' %d protein%s' %
                               (pathogenProteinCount,
                                '' if pathogenProteinCount == 1 else 's'))

            pathogenReadsFilename = join(
                self._pathogenDataDir,
                'pathogen-%s.%s' % (genomeAccession, self._format))

            pathogenReadsFp = open(pathogenReadsFilename, 'w')
            pathogenReadCount = 0

            append(
                '<a id="pathogen-%s"></a>'
                '<p class="pathogen">'
                '<span class="pathogen-name">%s</span> '
                '<span class="host">(%s)</span>'
                '<br/>%d nt, %s, '
                'matched by %d sample%s, '
                '<a href="%s">%s</a> in total. '
                '%s'
                '<br/><span class="taxonomy">Taxonomy: %s.</span>'
                '</p>' %
                (genomeAccession,
                 genomeInfo['organism'],
                 genomeInfo.get('host') or 'unknown host',
                 genomeInfo['length'],
                 proteinCountStr,
                 sampleCount, '' if sampleCount == 1 else 's',
                 pathogenReadsFilename, self.READCOUNT_MARKER,
                 pathogenLinksHTML,
                 lineageHTML))

            # Remember where we are in the output result so we can fill in
            # the total read count once we have processed all samples for
            # this pathogen. Not nice, I know.
            pathogenReadCountLineIndex = len(result) - 1

            for sampleName in sorted(samples):
                readsFileName = self.pathogenSampleFiles.lookup(
                    genomeAccession, sampleName)

                # Copy the read data from the per-sample reads for this
                # pathogen into the per-pathogen file of reads.
                with open(readsFileName) as readsFp:
                    while True:
                        data = readsFp.read(4096)
                        if data:
                            pathogenReadsFp.write(data)
                        else:
                            break

                proteins = samples[sampleName]['proteins']
                proteinCount = len(proteins)
                uniqueReadCount = samples[sampleName]['uniqueReadCount']
                pathogenReadCount += uniqueReadCount
                proteinCountHTML = '%d protein%s, ' % (
                    proteinCount, '' if proteinCount == 1 else 's')

                append(
                    '<p class="sample indented">'
                    'Sample <a href="#sample-%s">%s</a> '
                    '(%s<a href="%s">%d '
                    'read%s</a>, <a href="%s">panel</a>):</p>' %
                    (sampleName, sampleName,
                     proteinCountHTML,
                     readsFileName,
                     uniqueReadCount, '' if uniqueReadCount == 1 else 's',
                     self.sampleNames[sampleName]))
                append('<ul class="protein-list indented">')
                for proteinName in sorted(proteins):
                    proteinMatch = proteins[proteinName]
                    append(
                        '<li>'
                        '<span class="stats">'
                        '%(coverage).2f %(medianScore)6.2f %(bestScore)6.2f '
                        '%(readAndHspCountStr)3s'
                        % proteinMatch
                    )

                    if self._saveReadLengths:
                        append(' (%s)' % ', '.join(
                            map(str, sorted(proteinMatch['readLengths']))))

                    # Add the </span> with no intermediate whitespace
                    # because the 'stats' CSS class uses 'pre' on
                    # whitespace, which results in a newline when we use
                    # '\n'.join(result).
                    result[-1] += '</span>'

                    append(
                        '<span class="protein-name">'
                        '%(proteinName)s'
                        '</span> '
                        '(%(proteinLength)d aa,'
                        % proteinMatch)

                    if proteinMatch['proteinURL']:
                        append('<a href="%s">%s</a>, ' % (
                            proteinMatch['proteinURL'],
                            proteinMatch['accession']))

                    append(
                        '<a href="%(bluePlotFilename)s">blue plot</a>, '
                        '<a href="%(readsFilename)s">reads</a>)'
                        % proteinMatch)

                    append('</li>')

                append('</ul>')

            pathogenReadsFp.close()

            # Sanity check there's a read count marker text in our output
            # where we expect it.
            readCountLine = result[pathogenReadCountLineIndex]
            if readCountLine.find(self.READCOUNT_MARKER) == -1:
                raise ValueError(
                    'Could not find pathogen read count marker (%s) in result '
                    'index %d text (%s).' %
                    (self.READCOUNT_MARKER, pathogenReadCountLineIndex,
                     readCountLine))

            # Put the read count into the pathogen summary line we wrote
            # earlier, replacing the read count marker with the correct
            # text.
            result[pathogenReadCountLineIndex] = readCountLine.replace(
                self.READCOUNT_MARKER,
                '%d read%s' % (pathogenReadCount,
                               '' if pathogenReadCount == 1 else 's'))

        append('''
            <script>
              var tree = %s;
              $('#tree').treeview({
                  data: tree,
                  enableLinks: true,
                  levels: 0,
              });
           </script>
        ''' % taxonomyHierarchy.toJSON())

        # Write all samples (with pathogens (with proteins)).
        append('<hr>')
        append('<h1>Samples by pathogen</h1>')

        for sampleName in sampleNames:
            samplePathogenAccessions = sorted(
                (accession for accession in self.genomeAccessions
                 if sampleName in self.genomeAccessions[accession]),
                key=self._genomeName)

            if len(samplePathogenAccessions):
                append(
                    '<a id="sample-%s"></a>'
                    '<p class="sample">Sample '
                    '<span class="sample-name">%s</span> '
                    'matched proteins from %d pathogen%s, '
                    '<a href="%s">panel</a>:</p>' %
                    (sampleName, sampleName, len(samplePathogenAccessions),
                     '' if len(samplePathogenAccessions) == 1 else 's',
                     self.sampleNames[sampleName]))
            else:
                append(
                    '<a id="sample-%s"></a>'
                    '<p class="sample">Sample '
                    '<span class="sample-name">%s</span> '
                    'did not match anything.</p>' %
                    (sampleName, sampleName))
                continue

            for genomeAccession in samplePathogenAccessions:
                genomeInfo = self._db.findGenome(genomeAccession)
                readsFileName = self.pathogenSampleFiles.lookup(
                    genomeAccession, sampleName)
                proteins = self.genomeAccessions[genomeAccession][sampleName][
                    'proteins']
                uniqueReadCount = self.genomeAccessions[
                    genomeAccession][sampleName]['uniqueReadCount']
                proteinCount = len(proteins)
                pathogenProteinCount = genomeInfo['proteinCount']
                proteinCountStr = '%d/%d protein%s' % (
                    proteinCount, pathogenProteinCount,
                    '' if pathogenProteinCount == 1 else 's')

                pathogenLinksHTML = ' (%s' % NCBISequenceLink(genomeAccession)

                if pathogenType == 'viral' and not omitVirusLinks:
                    quoted = quote(genomeInfo['organism'])
                    pathogenLinksHTML += (
                        ', <a href="%s%s">ICTV</a>, '
                        '<a href="%s%s">ViralZone</a>)'
                    ) % (self.ICTV, quoted, self.VIRALZONE, quoted)
                else:
                    pathogenLinksHTML += ')'

                append(
                    '<p class="sample indented">'
                    '<a href="#pathogen-%s">%s</a> %s %s, '
                    '<a href="%s">%d read%s</a>:</p>' %
                    (genomeAccession, genomeInfo['organism'],
                     pathogenLinksHTML, proteinCountStr, readsFileName,
                     uniqueReadCount, '' if uniqueReadCount == 1 else 's'))
                append('<ul class="protein-list indented">')
                for proteinAccession in sorted(proteins):
                    proteinMatch = proteins[proteinAccession]
                    append(
                        '<li>'
                        '<span class="stats">'
                        '%(coverage).2f %(medianScore)6.2f %(bestScore)6.2f '
                        '%(readAndHspCountStr)3s'
                        '</span> '
                        '<span class="protein-name">'
                        '%(proteinName)s'
                        '</span> '
                        '(%(proteinLength)d aa,'
                        % proteinMatch)

                    if proteinMatch['proteinURL']:
                        append('<a href="%s">%s</a>, ' % (
                            proteinMatch['proteinURL'],
                            proteinMatch['accession']))

                    append(
                        '<a href="%(bluePlotFilename)s">blue plot</a>, '
                        '<a href="%(readsFilename)s">reads</a>)'
                        % proteinMatch)

                    append('</li>')

                append('</ul>')

        append('</body>')
        append('</html>')

        return '\n'.join(result)

    def _pathogenSamplePlot(self, genomeAccession, sampleNames, ax):
        """
        Make an image of a graph giving pathogen read count (Y axis) versus
        sample id (X axis).

        @param genomeAccession: A C{str} pathogen accession number.
        @param sampleNames: A sorted C{list} of sample names.
        @param ax: A matplotlib C{axes} instance.
        """
        readCounts = []
        for sampleName in sampleNames:
            try:
                readCount = self.genomeAccessions[genomeAccession][sampleName][
                    'uniqueReadCount']
            except KeyError:
                readCount = 0
            readCounts.append(readCount)

        highlight = 'r'
        normal = 'gray'
        sdMultiple = 2.5
        minReadsForHighlighting = 10
        highlighted = []

        if len(readCounts) == 1:
            if readCounts[0] > minReadsForHighlighting:
                color = [highlight]
                highlighted.append(sampleNames[0])
            else:
                color = [normal]
        else:
            mean = np.mean(readCounts)
            sd = np.std(readCounts)
            color = []
            for readCount, sampleName in zip(readCounts, sampleNames):
                if (readCount > (sdMultiple * sd) + mean and
                        readCount >= minReadsForHighlighting):
                    color.append(highlight)
                    highlighted.append(sampleName)
                else:
                    color.append(normal)

        nSamples = len(sampleNames)
        x = np.arange(nSamples)
        yMin = np.zeros(nSamples)
        ax.set_xticks([])
        ax.set_xlim((-0.5, nSamples - 0.5))
        ax.vlines(x, yMin, readCounts, color=color)
        if highlighted:
            title = '%s\nIn red: %s' % (
                genomeAccession, fill(', '.join(highlighted), 50))
        else:
            # Add a newline to keep the first line of each title at the
            # same place as those titles that have an "In red:" second
            # line.
            title = genomeAccession + '\n'

        ax.set_title(title, fontsize=10)
        ax.tick_params(axis='both', which='major', labelsize=8)
        ax.tick_params(axis='both', which='minor', labelsize=6)

    def pathogenPanel(self, filename):
        """
        Make a panel of images, with each image being a graph giving pathogen
        de-duplicated (by id) read count (Y axis) versus sample id (X axis).

        @param filename: A C{str} file name to write the image to.
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        self._computeUniqueReadCounts()
        genomeAccessions = sorted(self.genomeAccessions)
        sampleNames = sorted(self.sampleNames)

        cols = 5
        rows = int(len(genomeAccessions) / cols) + (
            0 if len(genomeAccessions) % cols == 0 else 1)
        figure, ax = plt.subplots(rows, cols, squeeze=False)

        coords = dimensionalIterator((rows, cols))

        for genomeAccession in genomeAccessions:
            row, col = next(coords)
            self._pathogenSamplePlot(genomeAccession, sampleNames,
                                     ax[row][col])

        # Hide the final panel graphs (if any) that have no content. We do
        # this because the panel is a rectangular grid and some of the
        # plots at the end of the last row may be unused.
        for row, col in coords:
            ax[row][col].axis('off')

        figure.suptitle(
            ('Per-sample read count for %d pathogen%s and %d sample%s.\n\n'
             'Sample name%s: %s') % (
                 len(genomeAccessions),
                 '' if len(genomeAccessions) == 1 else 's',
                 len(sampleNames),
                 '' if len(sampleNames) == 1 else 's',
                 '' if len(sampleNames) == 1 else 's',
                 fill(', '.join(sampleNames), 50)),
            fontsize=20)
        figure.set_size_inches(5.0 * cols, 2.0 * rows, forward=True)
        plt.subplots_adjust(hspace=0.4)

        figure.savefig(filename)


class _Genome(object):
    """
    Hold genome information, mirroring the attributes of a BioPython
    GenBank record.

    @param d: A C{dict} holding genome information (see below).
    """
    def __init__(self, d):
        self.id = d['id']
        self.description = d['name']
        self.seq = d['sequence']
        self.annotations = {
            'taxonomy': d['taxonomy'],
        }
        self.lineage = d.get('lineage')
        self.features = [_GenomeFeature(f) for f in d['features']]


class _GenomeLocation(object):
    """
    Hold genome feature location information, mirroring the attributes of a
    BioPython GenBank record.

    @param start: An C{int} start location.
    @param end: An C{int} stop location.
    """
    def __init__(self, start, end, strand):
        self.start = start
        self.end = end
        self.strand = strand

    def __str__(self):
        return '[%d:%d](%s)' % (self.start, self.end,
                                '+' if self.strand == 1 else '-')


class _GenomeFeature(object):
    """
    Hold genome feature information, mirroring the attributes of a BioPython
    GenBank record.

    @param d: A C{dict} holding genome feature information.
    """
    def __init__(self, d):
        self.type = d['type']
        self.qualifiers = d['qualifiers']
        self.strand = 1
        location = d['qualifiers']['location']
        self.location = _GenomeLocation(location['start'], location['stop'],
                                        self.strand)


class SqliteIndexWriter(object):
    """
    Create or update an Sqlite3 database holding information about proteins and
    the genomes they come from.

    @param dbFilename: A C{str} file name containing an sqlite3 database. If
        the file does not exist it will be created. The special string
        ":memory:" can be used to create an in-memory database.
    @param fastaFp: A file-pointer to which the protein FASTA is written.
    """
    PROTEIN_ACCESSION_FIELD = 2
    GENOME_ACCESSION_FIELD = 4
    TAXONOMY_SEPARATOR = '\t'
    SEQUENCE_ID_PREFIX = 'civ'
    SEQUENCE_ID_SEPARATOR = '|'

    def __init__(self, dbFilename, fastaFp=sys.stdout):
        self._connection = sqlite3.connect(dbFilename)
        self._fastaFp = fastaFp

        cur = self._connection.cursor()
        cur.executescript('''
            CREATE TABLE IF NOT EXISTS proteins (
                accession VARCHAR UNIQUE PRIMARY KEY,
                genomeAccession VARCHAR NOT NULL,
                sequence VARCHAR NOT NULL,
                length INTEGER NOT NULL,
                offsets VARCHAR NOT NULL,
                forward INTEGER NOT NULL,
                circular INTEGER NOT NULL,
                rangeCount INTEGER NOT NULL,
                gene VARCHAR,
                note VARCHAR,
                product VARCHAR,
                FOREIGN KEY (genomeAccession)
                    REFERENCES genomes (accession)
            );

            CREATE TABLE IF NOT EXISTS genomes (
                accession VARCHAR UNIQUE PRIMARY KEY,
                organism VARCHAR NOT NULL,
                name VARCHAR NOT NULL,
                sequence VARCHAR NOT NULL,
                length INTEGER NOT NULL,
                proteinCount INTEGER NOT NULL,
                host VARCHAR,
                note VARCHAR,
                taxonomyId INTEGER,
                taxonomy VARCHAR NOT NULL,
                databaseName VARCHAR
            );
            ''')
        self._connection.commit()

    def addGenBankFile(self, filename, taxonomyDatabase, rnaOnly=False,
                       excludeExclusiveHosts=None,
                       excludeFungusOnlyViruses=False,
                       excludePlantOnlyViruses=False, databaseName=None,
                       proteinSource='GENBANK', genomeSource='GENBANK',
                       duplicationPolicy='error', logfp=None):
        """
        Add proteins from a GenBank file.

        @param filename: A C{str} file name, with the file in GenBank format
            (see https://www.ncbi.nlm.nih.gov/Sitemap/samplerecord.html).
        @param taxonomyDatabase: A taxonomy database. Must be given if
            C{rnaOnly} is C{True} or C{excludeExclusiveHosts} is not C{None}.
        @param rnaOnly: If C{True}, only include RNA viruses.
        @param excludeExclusiveHosts: Either C{None} or a set of host types
            that should cause a genome to be excluded if the genome only
            has a single host and it is in C{excludeExclusiveHosts}.
        @param excludeFungusOnlyViruses: If C{True}, do not include fungus-only
            viruses.
        @param excludePlantOnlyViruses: If C{True}, do not include plant-only
            viruses.
        @param databaseName: A C{str} indicating the database the records
            in C{filename} came from (e.g., 'refseq' or 'RVDB').
        @param proteinSource: A C{str} giving the source of the protein
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param genomeSource: A C{str} giving the source of the genome
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param duplicationPolicy: A C{str} indicating what to do if a
            to-be-inserted accession number is already present in the database.
            "error" results in a ValueError being raised, "ignore" means ignore
            the duplicate. It should also be possible to update (i.e., replace)
            but that is not supported yet.
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @raise DatabaseDuplicationError: If a duplicate accession number is
            encountered and C{duplicationPolicy} is 'error'.
        @return: A tuple containing two C{int}s: the number of genome sequences
            in the added file and the total number of proteins found.
        """

        def lineageFetcher(genome):
            return taxonomyDatabase.lineage(genome.id)

        with open(filename) as fp:
            with self._connection:
                genomes = SeqIO.parse(fp, 'gb')
                return self._addGenomes(
                    genomes, taxonomyDatabase, lineageFetcher,
                    rnaOnly=rnaOnly,
                    excludeExclusiveHosts=excludeExclusiveHosts,
                    excludeFungusOnlyViruses=excludeFungusOnlyViruses,
                    excludePlantOnlyViruses=excludePlantOnlyViruses,
                    databaseName=databaseName, proteinSource=proteinSource,
                    genomeSource=genomeSource,
                    duplicationPolicy=duplicationPolicy, logfp=logfp)

    def addJSONFile(self, filename, taxonomyDatabase, rnaOnly=False,
                    excludeExclusiveHosts=None,
                    excludeFungusOnlyViruses=False,
                    excludePlantOnlyViruses=False,
                    databaseName=None, proteinSource='GENBANK',
                    genomeSource='GENBANK', duplicationPolicy='error',
                    logfp=None):
        """
        Add proteins from a JSON infor file.

        @param filename: A C{str} file name, in JSON format.
        @param taxonomyDatabase: A taxonomy database. Must be given if
            C{rnaOnly} is C{True} or C{excludeExclusiveHosts} is not C{None}.
        @param rnaOnly: If C{True}, only include RNA viruses.
        @param excludeExclusiveHosts: Either C{None} or a set of host types
            that should cause a genome to be excluded if the genome only
            has a single host and it is in C{excludeExclusiveHosts}.
        @param excludeFungusOnlyViruses: If C{True}, do not include fungus-only
            viruses.
        @param excludePlantOnlyViruses: If C{True}, do not include plant-only
            viruses.
        @param databaseName: A C{str} indicating the database the records
            in C{filename} came from (e.g., 'refseq' or 'RVDB').
        @param proteinSource: A C{str} giving the source of the protein
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param genomeSource: A C{str} giving the source of the genome
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param duplicationPolicy: A C{str} indicating what to do if a
            to-be-inserted accession number is already present in the database.
            "error" results in a ValueError being raised, "ignore" means ignore
            the duplicate. It should also be possible to update (i.e., replace)
            but that is not supported yet.
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @raise DatabaseDuplicationError: If a duplicate accession number is
            encountered and C{duplicationPolicy} is 'error'.
        @return: A tuple containing two C{int}s: the number of genome sequences
            in the added file and the total number of proteins found.
        """

        def lineageFetcher(genome):
            return genome.lineage

        with open(filename) as fp:
            genome = _Genome(load(fp))

        with self._connection:
            return self._addGenomes(
                [genome], taxonomyDatabase, lineageFetcher,
                rnaOnly=rnaOnly,
                excludeExclusiveHosts=excludeExclusiveHosts,
                excludeFungusOnlyViruses=excludeFungusOnlyViruses,
                excludePlantOnlyViruses=excludePlantOnlyViruses,
                databaseName=databaseName, proteinSource=proteinSource,
                genomeSource=genomeSource,
                duplicationPolicy=duplicationPolicy, logfp=logfp)

    def _addGenomes(
            self, genomes, taxonomyDatabase, lineageFetcher, rnaOnly=False,
            excludeExclusiveHosts=None, excludeFungusOnlyViruses=False,
            excludePlantOnlyViruses=False, databaseName=None,
            proteinSource='GENBANK', genomeSource='GENBANK',
            duplicationPolicy='error', logfp=None):
        """
        Add a bunch of genomes.

        @param genomes: An iterable of genomes. These are either genomes
            returned by BioPython's GenBank parser or instances of C{_Genome}.
        @param taxonomyDatabase: A taxonomy database.
        @param lineageFetcher: A function that takes a genome and returns a
            C{tuple} of the taxonomic categories of the genome. Each
            tuple element is a 3-tuple of (C{int}, C{str}, C{str}) giving a
            taxonomy id a (scientific) name, and the rank (species, genus,
            etc). I.e., as returned by L{dark.taxonomy.LineageFetcher.lineage}.
        @param rnaOnly: If C{True}, only include RNA viruses.
        @param excludeExclusiveHosts: Either C{None} or a set of host types
            that should cause a genome to be excluded if the genome only
            has a single host and it is in C{excludeExclusiveHosts}.
        @param excludeFungusOnlyViruses: If C{True}, do not include fungus-only
            viruses.
        @param excludePlantOnlyViruses: If C{True}, do not include plant-only
            viruses.
        @param databaseName: A C{str} indicating the database the records
            in C{filename} came from (e.g., 'refseq' or 'RVDB').
        @param proteinSource: A C{str} giving the source of the protein
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param genomeSource: A C{str} giving the source of the genome
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param duplicationPolicy: A C{str} indicating what to do if a
            to-be-inserted accession number is already present in the database.
            "error" results in a ValueError being raised, "ignore" means ignore
            the duplicate. It should also be possible to update (i.e., replace)
            but that is not supported yet.
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @raise DatabaseDuplicationError: If a duplicate accession number is
            encountered and C{duplicationPolicy} is 'error'.
        @return: A tuple containing two C{int}s: the number of genome sequences
            in the added file and the total number of proteins found.
        """
        assert self.SEQUENCE_ID_SEPARATOR not in proteinSource, (
            'proteinSource cannot contain %r as that is used as a separator.' %
            self.SEQUENCE_ID_SEPARATOR)

        assert self.SEQUENCE_ID_SEPARATOR not in genomeSource, (
            'genomeSource cannot contain %r as that is used as a separator.' %
            self.SEQUENCE_ID_SEPARATOR)

        genomeCount = totalProteinCount = 0

        for genome in genomes:
            if logfp:
                print('\n%s: %s' % (genome.id, genome.description), file=logfp)
                for k, v in genome.annotations.items():
                    if k not in ('references', 'comment',
                                 'structured_comment'):
                        print('  %s = %r' % (k, v), file=logfp)

            try:
                lineage = lineageFetcher(genome)
            except ValueError as e:
                print('ValueError calling lineage fetcher: %s' % e,
                      file=sys.stderr)
                lineage = taxonomyId = None
            else:
                taxonomyId = lineage[0][0]

            if rnaOnly:
                if lineage:
                    print('  Lineage:', file=logfp)
                    print(formatLineage(lineage, prefix='    '), file=logfp)
                    if not isRNAVirus(lineage):
                        if logfp:
                            print('  %s (%s) is not an RNA virus. Skipping.' %
                                  (genome.id, genome.description), file=logfp)
                        continue
                    else:
                        if logfp:
                            print('  %s (%s) is an RNA virus.' %
                                  (genome.id, genome.description), file=logfp)
                else:
                    print('Could not look up taxonomy lineage for %s (%s). '
                          'Cannot confirm as RNA. Skipping.' %
                          (genome.id, genome.description), file=logfp)
                    continue

            if excludeFungusOnlyViruses:
                if lineage is None:
                    print('Could not look up taxonomy lineage for %s '
                          '(%s). Cannot confirm as fungus-only virus. '
                          'Skipping.' %
                          (genome.id, genome.description), file=logfp)
                else:
                    if taxonomyDatabase.isFungusOnlyVirus(
                            lineage, genome.description):
                        if logfp:
                            print('  %s (%s) is a fungus-only virus.' %
                                  (genome.id, genome.description), file=logfp)
                        continue
                    else:
                        if logfp:
                            print('  %s (%s) is not a fungus-only virus.' %
                                  (genome.id, genome.description), file=logfp)

            if excludePlantOnlyViruses:
                if lineage is None:
                    print('Could not look up taxonomy lineage for %s '
                          '(%s). Cannot confirm as plant-only virus. '
                          'Skipping.' %
                          (genome.id, genome.description), file=logfp)
                else:
                    if taxonomyDatabase.isPlantOnlyVirus(
                            lineage, genome.description):
                        if logfp:
                            print('  %s (%s) is a plant-only virus.' %
                                  (genome.id, genome.description), file=logfp)
                        continue
                    else:
                        if logfp:
                            print('  %s (%s) is not a plant-only virus.' %
                                  (genome.id, genome.description), file=logfp)

            if excludeExclusiveHosts:
                if taxonomyId is None:
                    print('Could not find taxonomy id for %s (%s). '
                          'Cannot exclude due to exclusive host criteria.' %
                          (genome.id, genome.description), file=logfp)
                else:
                    hosts = taxonomyDatabase.hosts(taxonomyId)
                    if hosts is None:
                        print('Could not find hosts for %s (%s). Cannot '
                              'exclude due to exclusive host criteria.' %
                              (genome.id, genome.description), file=logfp)
                    else:
                        if (len(hosts) == 1 and
                                hosts.pop() in excludeExclusiveHosts):
                            print('Excluding %s (%s) due to exclusive host '
                                  'criteria.' %
                                  (genome.id, genome.description), file=logfp)
                            continue

            proteinCount = len(list(self._genomeProteins(genome)))

            if self.addGenome(
                    genome, taxonomyId, proteinCount, databaseName,
                    duplicationPolicy=duplicationPolicy, logfp=logfp):

                self.addProteins(
                    genome, proteinSource=proteinSource,
                    genomeSource=genomeSource,
                    duplicationPolicy=duplicationPolicy, logfp=logfp)

                totalProteinCount += proteinCount
                genomeCount += 1

                print('  Added %s (%s) with %d protein%s to database.' %
                      (genome.id, genome.description, proteinCount,
                       '' if proteinCount == 1 else 's'), file=logfp)

        return genomeCount, totalProteinCount

    def addGenome(self, genome, taxonomyId, proteinCount, databaseName,
                  duplicationPolicy='error', logfp=None):
        """
        Add information about a genome to the genomes table.

        @param genome: A GenBank genome record, as parsed by SeqIO.parse
        @param taxonomyId: Either an C{int} taxonomy id or C{None} if the
            genome taxonomy could not be looked up.
        @param proteinCount: The C{int} number of proteins in the genome.
        @param databaseName: A C{str} indicating the database the records
            in C{filename} came from (e.g., 'refseq' or 'RVDB').
        @param duplicationPolicy: A C{str} indicating what to do if a
            to-be-inserted accession number is already present in the database.
            "error" results in a ValueError being raised, "ignore" means ignore
            the duplicate. It should also be possible to update (i.e., replace)
            but that is not supported yet.
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @raise DatabaseDuplicationError: If a duplicate accession number is
            encountered and C{duplicationPolicy} is 'error'.
        @return: C{True} if the genome was added, else C{False}.
        """
        sequence = str(genome.seq)
        taxonomy = self.TAXONOMY_SEPARATOR.join(genome.annotations['taxonomy'])
        source = self._sourceInfo(genome, logfp=logfp)

        if source is None:
            # The lack of a source is logged by self._sourceInfo.
            return False

        try:
            self._connection.execute(
                'INSERT INTO genomes(accession, organism, name, sequence, '
                'length, proteinCount, host, note, taxonomyId, taxonomy, '
                'databaseName) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (genome.id, source['organism'], genome.description,
                 sequence, len(sequence), proteinCount, source['host'],
                 source.get('note'), taxonomyId, taxonomy, databaseName))
        except sqlite3.IntegrityError as e:
            if str(e).find('UNIQUE constraint failed') > -1:
                if duplicationPolicy == 'error':
                    raise DatabaseDuplicationError(
                        'Genome information for %r already present in '
                        'database.' % genome.id)
                elif duplicationPolicy == 'ignore':
                    if logfp:
                        print(
                            'Genome information for %r already present in '
                            'database. Ignoring.' % genome.id, file=logfp)
                    return False
                else:
                    raise NotImplementedError(
                        'Unknown duplication policy (%s) found when '
                        'attempting to insert genome information for %s.' %
                        (duplicationPolicy, genome.id))
            else:
                raise
        else:
            return True

    def addProteins(self, genome, proteinSource='GENBANK',
                    genomeSource='GENBANK', duplicationPolicy='error',
                    logfp=None):
        """
        Add proteins from a Genbank genome record to the proteins database and
        write out their sequences to the proteins FASTA file (in
        C{self._fastaFp}).

        @param genome: Either a GenBank genome record, as parsed by
            C{SeqIO.parse} or a C{_Genome} instance (which behaves like the
            former).
        @param proteinSource: A C{str} giving the source of the protein
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param genomeSource: A C{str} giving the source of the genome
            accession number. This becomes part of the sequence id printed
            in the protein FASTA output.
        @param duplicationPolicy: A C{str} indicating what to do if a
            to-be-inserted accession number is already present in the database.
            "error" results in a ValueError being raised, "ignore" means ignore
            the duplicate. It should also be possible to update (i.e., replace)
            but that is not supported yet.
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @raise DatabaseDuplicationError: If a duplicate accession number is
            encountered and C{duplicationPolicy} is 'error'.
        """
        genomeLen = len(genome.seq)
        source = self._sourceInfo(genome, logfp=logfp)
        # source must be present. addGenome would skip this genome otherwise.
        assert source

        for fInfo in self._genomeProteins(genome, logfp=logfp):

            # Write FASTA for the protein.
            seqId = self.SEQUENCE_ID_SEPARATOR.join((
                self.SEQUENCE_ID_PREFIX,
                proteinSource, fInfo['proteinId'],
                genomeSource, genome.id,
                fInfo['product']))

            print('>%s [%s]\n%s' %
                  (seqId, source['organism'], fInfo['translation']),
                  file=self._fastaFp)

            self.addProtein(
                fInfo['proteinId'], genome.id, fInfo['translation'],
                fInfo['featureLocation'], fInfo['forward'],
                fInfo['circular'],
                fInfo['ranges'].distinctRangeCount(genomeLen),
                gene=fInfo['gene'], note=fInfo['note'],
                product=fInfo['product'], duplicationPolicy=duplicationPolicy,
                logfp=logfp)

    def addProtein(self, accession, genomeAccession, sequence, offsets,
                   forward, circular, rangeCount, gene=None, note=None,
                   product=None, duplicationPolicy='error', logfp=None):
        """
        Add information about a protein to the proteins table.

        @param accession: A C{str} protein accession id.
        @param genomeAccession: A C{str} genome accession id (the genome to
            which this protein belongs).
        @param sequence: A C{str} protein amino acid sequence.
        @param offsets: A C{str} describing the offsets of the protein in the
            genome (as obtained from C{SeqIO.parse} on a GenBank file).
        @param forward: A C{bool}, C{True} if the protein occurs on the
            forward strand of the genome, C{False} if on the complement strand.
            Note that this is converted to an C{int} in the database.
        @param circular: A C{bool}, C{True} if the protein crosses the genome
            boundary and is therefore circular, C{False} if not. Note that
            this is converted to an C{int} in the database.
        @param rangeCount: The C{int} number of ranges (regions) the protein
            comes from in the genome.
        @param gene: A C{str} gene name, or C{None} if no gene is known.
        @param note: A C{str} note about the protein, or C{None}.
        @param product: A C{str} description of the protein product (e.g.,
            "putative replication initiation protein"), or C{None}.
        @param duplicationPolicy: A C{str} indicating what to do if a
            to-be-inserted accession number is already present in the database.
            "error" results in a ValueError being raised, "ignore" means ignore
            the duplicate. It should also be possible to update (i.e., replace)
            but that is not supported yet.
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @raise DatabaseDuplicationError: If a duplicate accession number is
            encountered and C{duplicationPolicy} is 'error'.
        """
        try:
            self._connection.execute(
                'INSERT INTO proteins('
                'accession, genomeAccession, sequence, length, offsets, '
                'forward, circular, rangeCount, gene, note, product) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (accession, genomeAccession, sequence, len(sequence), offsets,
                 int(forward), int(circular), rangeCount, gene, note, product))
        except sqlite3.IntegrityError as e:
            if str(e).find('UNIQUE constraint failed') > -1:
                if duplicationPolicy == 'error':
                    raise DatabaseDuplicationError(
                        'Protein information for %r already present in '
                        'database.' % accession)
                elif duplicationPolicy == 'ignore':
                    if logfp:
                        print(
                            'Protein information for %r already present in '
                            'database. Ignoring.' % accession, file=logfp)
                else:
                    raise NotImplementedError(
                        'Unknown duplication policy (%s) found when '
                        'attempting to insert protein information for %s.' %
                        (duplicationPolicy, accession))
            else:
                raise
        else:
            if logfp:
                print('    Protein %s: genome=%s product=%s' % (
                    accession, genomeAccession, product), file=logfp)

    def _sourceInfo(self, genome, logfp):
        """
        Extract summary information from a genome source feature.

        @param genome: A GenBank genome record, as parsed by SeqIO.parse
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @return: A C{dict} with keys for the various pieces of information
            (if any) found in the source feature (see the return value below
            for detail). Or C{None} if no source feature is found.
        """
        result = {}

        for feature in genome.features:
            if feature.type == 'source':
                for key in 'host', 'note', 'organism':
                    try:
                        values = feature.qualifiers[key]
                    except KeyError:
                        value = None
                    else:
                        assert len(values) == 1
                        value = values[0]

                    result[key] = value
                break
        else:
            warn('Genome %r (accession %s) had no source feature! '
                 'Skipping.' % (genome.description, genome.id))
            return

        return result

    def _cdsInfo(self, genome, feature, logfp=None):
        """
        Extract summary information from a genome CDS feature.

        @param genome: A GenBank genome record, as parsed by SeqIO.parse
        @param feature: A feature from a genome, as produced by BioPython's
            GenBank parser.
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @return: A C{dict} with keys for the various pieces of information
            found in the feature (see the return value below for detail).
            Or C{None} if the feature is not of interest or otherwise invalid.
        """
        qualifiers = feature.qualifiers

        # Check in advance that all feature qualifiers we're interested in
        # have the right lengths, if they're present.
        for key in 'gene', 'note', 'product', 'protein_id', 'translation':
            if key in qualifiers:
                assert len(qualifiers[key]) == 1, (
                    'GenBank qualifier key %s is not length one %r' %
                    (key, qualifiers[key]))

        # A protein id is mandatory.
        if 'protein_id' in qualifiers:
            proteinId = qualifiers['protein_id'][0]
        else:
            if 'translation' in qualifiers:
                warn('Genome %r (accession %s) has CDS feature with no '
                     'protein_id feature but has a translation! '
                     'Skipping.\nFeature: %s' %
                     (genome.description, genome.id, feature))
            return

        # A translated (i.e., amino acid) sequence is mandatory.
        if 'translation' in qualifiers:
            translation = qualifiers['translation'][0]
        else:
            warn('Genome %r (accession %s) has CDS feature with protein '
                 '%r with no translated sequence. Skipping.' %
                 (genome.description, genome.id, proteinId))
            return

        featureLocation = str(feature.location)

        # Make sure the feature's location string can be parsed.
        try:
            ranges = GenomeRanges(featureLocation)
        except ValueError as e:
            warn('Genome %r  (accession %s) contains unparseable CDS '
                 'location for protein %r. Skipping. Error: %s' %
                 (genome.description, genome.id, proteinId, e))
            return
        else:
            # Does the protein span the end of the genome? This indicates a
            # circular genome.
            circular = int(ranges.circular(len(genome.seq)))

        if feature.location.start >= feature.location.end:
            warn('Genome %r (accession %s) contains feature with start '
                 '(%d) >= stop (%d). Skipping.\nFeature: %s' %
                 (genome.description, genome.id, feature.location.start,
                  feature.location.end, feature))
            return

        strand = feature.strand
        if strand is None:
            # The strands of the protein in the genome are not all the same
            # (see Bio.SeqFeature.CompoundLocation._get_strand).  The
            # protein is formed by the combination of reading one strand in
            # one direction and the other in the other direction.
            #
            # This occurs just once in all 1.17M proteins found in all 700K
            # RVDB (C-RVDBv15.1) genomes, for protein YP_656697.1 on the
            # Ranid herpesvirus 1 strain McKinnell genome (NC_008211.1).
            #
            # This situation makes turning DIAMOND protein output into
            # SAM very complicated because a match on such a protein
            # cannot be stored as a SAM linear alignment. It instead
            # requires a multi-line 'supplementary' alignment. The code
            # and tests for that are more complex than I want to deal
            # with at the moment, just for the sake of one protein in a
            # frog herpesvirus.
            warn('Genome %s (accession %s) has protein %r with mixed '
                 'orientation!' % (genome.description, genome.id,
                                   proteinId))
            return
        elif strand == 0:
            # This never occurs for proteins corresponding to genomes in
            # the RVDB database C-RVDBv15.1.
            warn('Genome %r (accession %s) has protein %r with feature '
                 'with strand of zero!' %
                 (genome.description, genome.id, proteinId))
            return
        else:
            assert strand in (1, -1)
            forward = strand == 1
            # Make sure the strand agrees with the orientations in the
            # string BioPython makes out of the locations.
            assert ranges.orientations() == {forward}

        return {
            'circular': circular,
            'featureLocation': featureLocation,
            'forward': forward,
            'gene': qualifiers.get('gene', [''])[0],
            'note': qualifiers.get('note', [''])[0],
            'product': qualifiers.get('product', ['UNKNOWN'])[0],
            'proteinId': proteinId,
            'ranges': ranges,
            'strand': strand,
            'translation': translation,
        }

    def _genomeProteins(self, genome, logfp=None):
        """
        Get proteins (CDS features) that we can process from a genome, along
        with information extracted from each.

        @param genome: A GenBank genome record, as parsed by SeqIO.parse
        @param logfp: If not C{None}, a file pointer to write verbose
            progress output to.
        @return: A generator yielding feature info C{dict}s as returned by
            C{self._cdsInfo}.
        """
        for feature in genome.features:
            if feature.type == 'CDS':
                featureInfo = self._cdsInfo(genome, feature, logfp=None)
                if featureInfo:
                    yield featureInfo

    def close(self):
        """
        Create indices on the accesssion ids and close the connection.
        """
        cur = self._connection.cursor()
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS protein_idx ON '
                    'proteins(accession)')
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS genomes_idx ON '
                    'genomes(accession)')
        self._connection.commit()
        self._connection.close()
        self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, excType, excValue, traceback):
        self.close()


class SqliteIndex(object):
    """
    Provide lookup access to an Sqlite3 database holding information about
    proteins and the genomes they come from.

    @param dbFilenameOrConnection: Either a C{str} file name containing an
        sqlite3 database as created by C{SqliteIndexWriter} or an already
        open connection to such a database. Note that an already open
        connection will not be closed by self.close().
    @param lookupCacheSize: The C{int} size of the memoization cache
        for the protein and genome lookup functions (each has its own
        memoization cache).
    """
    PROTEIN_ACCESSION_FIELD = 2
    GENOME_ACCESSION_FIELD = 4

    def __init__(self, dbFilenameOrConnection, lookupCacheSize=1024):
        if isinstance(dbFilenameOrConnection, string_types):
            self._connection = sqlite3.connect(dbFilenameOrConnection)
            self._closeConnection = True
        else:
            self._connection = dbFilenameOrConnection
            self._closeConnection = False
        self._connection.row_factory = sqlite3.Row
        self._proteinCache = LRUCache(maxsize=lookupCacheSize)
        self._genomeCache = LRUCache(maxsize=lookupCacheSize)

    def genomeAccession(self, id_):
        """
        Get the genome accession info from a sequence id.

        @param id_: A C{str} sequence id in the form
            'civ|GENBANK|%s|GENBANK|%s|%s [%s]' where the genome accession
            is in the fifth '|'-separated field.
        @raise IndexError: If C{id_} does not have enough |-separated fields.
        @return: The C{str} accession number.
        """
        return id_.split('|', self.GENOME_ACCESSION_FIELD + 1)[
            self.GENOME_ACCESSION_FIELD]

    def proteinAccession(self, id_):
        """
        Get the protein accession info from a sequence id.

        @param id_: A C{str} sequence id in the form
            'civ|GENBANK|%s|GENBANK|%s|%s [%s]' where the protein accession
            is in the third '|'-separated field.
        @raise IndexError: If C{id_} does not have enough |-separated fields.
        @return: The C{str} accession number.
        """
        return id_.split('|', self.PROTEIN_ACCESSION_FIELD + 1)[
            self.PROTEIN_ACCESSION_FIELD]

    @cachedmethod(attrgetter('_genomeCache'))
    def _findGenome(self, accession):
        """
        Find info about a genome, given an accession number.

        @param accession: A C{str} accession number.
        @return: A C{dict} with keys corresponding to the names of the columns
            in the genomes database table, else C{None} if C{id_} cannot be
            found.
        """
        cur = self.execute(
            'SELECT * FROM genomes WHERE accession = ?', (accession,))
        row = cur.fetchone()

        if row:
            result = dict(row)
            result['accession'] = accession
            result['taxonomy'] = result['taxonomy'].split(
                SqliteIndexWriter.TAXONOMY_SEPARATOR)
            return result

    def findGenome(self, id_):
        """
        Find info about a genome, given a sequence id.

        @param id_: A C{str} sequence id. This is either of the form
            'civ|GENBANK|%s|GENBANK|%s|%s [%s]' where the genome id is in the
            5th '|'-delimited field, or else is the nucleotide sequence
            accession number as already extracted.
        @return: A C{dict} with keys corresponding to the names of the columns
            in the genomes database table, else C{None} if C{id_} cannot be
            found.
        """
        try:
            accession = self.genomeAccession(id_)
        except IndexError:
            accession = id_

        return self._findGenome(accession)

    @cachedmethod(attrgetter('_proteinCache'))
    def _findProtein(self, accession):
        """
        Find info about a protein, given an accession number.

        @param accession: A C{str} accession number.
        @return: A C{dict} with keys corresponding to the names of the columns
            in the proteins database table, else C{None} if C{id_} cannot be
            found.
        """
        cur = self.execute(
            'SELECT * FROM proteins WHERE accession = ?', (accession,))
        row = cur.fetchone()
        if row:
            result = dict(row)
            result['forward'] = bool(result['forward'])
            result['circular'] = bool(result['circular'])
            result['length'] = int(result['length'])
            result['accession'] = accession
            return result

    def findProtein(self, id_):
        """
        Find info about a protein, given a sequence id.

        @param id_: A C{str} sequence id. This is either of the form
            'civ|GENBANK|%s|GENBANK|%s|%s [%s]' where the protein id is in the
            3rd '|'-delimited field, or else is the protein accession number as
            already extracted.
        @return: A C{dict} with keys corresponding to the names of the columns
            in the proteins database table, else C{None} if C{id_} cannot be
            found.
        """
        try:
            accession = self.proteinAccession(id_)
        except IndexError:
            accession = id_

        return self._findProtein(accession)

    def execute(self, query, *args):
        """
        Execute an SQL statement. See
        https://docs.python.org/3.5/library/sqlite3.html#sqlite3.Cursor.execute
        for full argument details.

        @param query: A C{str} SQL query.
        @param args: Additional arguments (if any) to pass to the sqlite3
            execute command.
        @return: An sqlite3 cursor.
        """
        cur = self._connection.cursor()
        cur.execute(query, *args)
        return cur

    def proteinCount(self):
        """
        How many proteins are in the database?

        @return: An C{int} protein count.
        """
        cur = self.execute('SELECT COUNT(1) FROM proteins')
        return int(cur.fetchone()[0])

    def genomeCount(self):
        """
        How many genomes are in the database?

        @return: An C{int} genome count.
        """
        cur = self.execute('SELECT COUNT(1) FROM genomes')
        return int(cur.fetchone()[0])

    def close(self):
        """
        Close the database connection (if we opened it).
        """
        if self._closeConnection:
            self._connection.close()
        self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, excType, excValue, traceback):
        self.close()
