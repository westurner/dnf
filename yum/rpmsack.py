#!/usr/bin/python -tt
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import rpm
import types
import warnings
import glob
import os
import os.path

from rpmUtils import miscutils
from rpmUtils import arch
from rpmUtils.transaction import initReadOnlyTransaction
import misc
import Errors
from packages import YumInstalledPackage, parsePackages
from packageSack import PackageSackBase, PackageSackVersion

# For returnPackages(patterns=)
import fnmatch
import re

from yum.i18n import to_unicode, _
import constants

import yum.depsolve

class RPMInstalledPackage(YumInstalledPackage):

    def __init__(self, rpmhdr, index, rpmdb):
        YumInstalledPackage.__init__(self, rpmhdr, yumdb=rpmdb.yumdb)
        # NOTE: We keep summary/description/url because it doesn't add much
        # and "yum search" uses them all.
        self.url       = rpmhdr['url']
        # Also keep sourcerpm for pirut/etc.
        self.sourcerpm = rpmhdr['sourcerpm']

        self.idx   = index
        self.rpmdb = rpmdb

        self._has_hdr = False
        del self.hdr

    def _get_hdr(self):
        # Note that we can't use hasattr(self, 'hdr') or we'll recurse
        if self._has_hdr:
            return self.hdr

        ts = self.rpmdb.readOnlyTS()
        mi = ts.dbMatch(0, self.idx)
        try:
            return mi.next()
        except StopIteration:
            raise Errors.PackageSackError, 'Rpmdb changed underneath us'

    def __getattr__(self, varname):
        self.hdr = val = self._get_hdr()
        self._has_hdr = True
        # If these existed, then we wouldn't get here ... and nothing in the DB
        # starts and ends with __'s. So these are missing.
        if varname.startswith('__') and varname.endswith('__'):
            raise AttributeError, "%s has no attribute %s" % (self, varname)
            
        if varname != 'hdr':   #  This is unusual, for anything that happens
            val = val[varname] # a lot we should preload at __init__.
                               # Also note that pkg.no_value raises KeyError.

        return val


class RPMDBProblem:
    '''
    Represents a problem in the rpmdb, from the check_*() functions.
    '''
    def __init__(self, pkg, problem, **kwargs):
        self.pkg = pkg
        self.problem = problem
        for kwarg in kwargs:
            setattr(self, kwarg, kwargs[kwarg])

    def __cmp__(self, other):
        if other is None:
            return 1
        return cmp(self.pkg, other.pkg) or cmp(self.problem, problem)


class RPMDBProblemDependency(RPMDBProblem):
    def __str__(self):
        if self.problem == 'requires':
            return "%s %s %s" % (self.pkg, _('has missing requires of'),
                                 self.missing)

        return "%s %s %s: %s" % (self.pkg, _('has installed conflicts'),
                                 self.found,', '.join(map(str, self.conflicts)))


class RPMDBProblemDuplicate(RPMDBProblem):
    def __init__(self, pkg, **kwargs):
        RPMDBProblem.__init__(self, pkg, "duplicate", **kwargs)

    def __str__(self):
        return _("%s is a duplicate with %s") % (self.pkg, self.duplicate)


class RPMDBPackageSack(PackageSackBase):
    '''
    Represent rpmdb as a packagesack
    '''

    DEP_TABLE = { 
            'requires'  : (rpm.RPMTAG_REQUIRENAME,
                           rpm.RPMTAG_REQUIREVERSION,
                           rpm.RPMTAG_REQUIREFLAGS),
            'provides'  : (rpm.RPMTAG_PROVIDENAME,
                           rpm.RPMTAG_PROVIDEVERSION,
                           rpm.RPMTAG_PROVIDEFLAGS),
            'conflicts' : (rpm.RPMTAG_CONFLICTNAME,
                           rpm.RPMTAG_CONFLICTVERSION,
                           rpm.RPMTAG_CONFLICTFLAGS),
            'obsoletes' : (rpm.RPMTAG_OBSOLETENAME,
                           rpm.RPMTAG_OBSOLETEVERSION,
                           rpm.RPMTAG_OBSOLETEFLAGS)
            }

    # Do we want to cache rpmdb data in a file, for later use?
    __cache_rpmdb__ = True

    def __init__(self, root='/', releasever=None, cachedir=None,
                 persistdir='/var/lib/yum'):
        self.root = root
        self._idx2pkg = {}
        self._name2pkg = {}
        self._tup2pkg = {}
        self._completely_loaded = False
        self._simple_pkgtup_list = []
        self._get_pro_cache = {}
        self._get_req_cache  = {}
        self._loaded_gpg_keys = False
        if cachedir is None:
            cachedir = misc.getCacheDir()
        self.setCacheDir(cachedir)
        self._persistdir = root +  '/' + persistdir
        self._have_cached_rpmdbv_data = None
        self._cached_conflicts_data = None
        # Store the result of what happens, if a transaction completes.
        self._trans_cache_store = {}
        self.ts = None
        self.releasever = releasever
        self.auto_close = False # this forces a self.ts.close() after
                                     # most operations so it doesn't leave
                                     # any lingering locks.

        self._cache = {
            'provides' : { },
            'requires' : { },
            'conflicts' : { },
            'obsoletes' : { },
            }
        
        addldb_path = os.path.normpath(self._persistdir + '/yumdb')
        self.yumdb = RPMDBAdditionalData(db_path=addldb_path)

    def _get_pkglist(self):
        '''Getter for the pkglist property. 
        Returns a list of package tuples.
        '''
        if not self._simple_pkgtup_list:
            for (hdr, mi) in self._all_packages():
                self._simple_pkgtup_list.append(self._hdr2pkgTuple(hdr))
            
        return self._simple_pkgtup_list

    pkglist = property(_get_pkglist, None)

    def dropCachedData(self):
        self._idx2pkg = {}
        self._name2pkg = {}
        self._tup2pkg = {}
        self._completely_loaded = False
        self._simple_pkgtup_list = []
        self._get_pro_cache = {}
        self._get_req_cache = {}
        #  We can be called on python shutdown (due to yb.__del__), at which
        # point other modules might not be available.
        if misc is not None:
            misc.unshare_data()
        self._cache = {
            'provides' : { },
            'requires' : { },
            'conflicts' : { },
            'obsoletes' : { },
            }
        self._have_cached_rpmdbv_data = None
        self._cached_conflicts_data = None

    def setCacheDir(self, cachedir):
        """ Sets the internal cachedir value for the rpmdb, to be the
            "installed" directory from this parent. """
        self._cachedir = self.root + '/' + cachedir + "/installed/"

    def readOnlyTS(self):
        if not self.ts:
            self.ts =  initReadOnlyTransaction(root=self.root)
        if not self.ts.open:
            self.ts = initReadOnlyTransaction(root=self.root)
        return self.ts

    def buildIndexes(self):
        # Not used here
        return

    def _checkIndexes(self, failure='error'):
        # Not used here
        return

    def delPackage(self, obj):
        # Not supported with this sack type
        pass

    def searchAll(self, name, query_type='like'):
        ts = self.readOnlyTS()
        result = {}

        # check provides
        tag = self.DEP_TABLE['provides'][0]
        mi = ts.dbMatch()
        mi.pattern(tag, rpm.RPMMIRE_GLOB, name)
        for hdr in mi:
            if hdr['name'] == 'gpg-pubkey':
                continue
            pkg = self._makePackageObject(hdr, mi.instance())
            if not result.has_key(pkg.pkgid):
                result[pkg.pkgid] = pkg
        del mi

        fileresults = self.searchFiles(name)
        for pkg in fileresults:
            if not result.has_key(pkg.pkgid):
                result[pkg.pkgid] = pkg
        
        if self.auto_close:
            self.ts.close()

        return result.values()

    def searchFiles(self, name):
        """search the filelists in the rpms for anything matching name"""

        ts = self.readOnlyTS()
        result = {}
        
        mi = ts.dbMatch('basenames', name)
        for hdr in mi:
            if hdr['name'] == 'gpg-pubkey':
                continue
            pkg = self._makePackageObject(hdr, mi.instance())
            if not result.has_key(pkg.pkgid):
                result[pkg.pkgid] = pkg
        del mi

        result = result.values()

        if self.auto_close:
            self.ts.close()

        return result
        
    def searchPrco(self, name, prcotype):

        result = self._cache[prcotype].get(name)
        if result is not None:
            return result

        ts = self.readOnlyTS()
        result = {}
        tag = self.DEP_TABLE[prcotype][0]
        mi = ts.dbMatch(tag, misc.to_utf8(name))
        for hdr in mi:
            if hdr['name'] == 'gpg-pubkey':
                continue
            po = self._makePackageObject(hdr, mi.instance())
            result[po.pkgid] = po
        del mi

        # If it's not a provides or filename, we are done
        if prcotype == 'provides' and name[0] == '/':
            fileresults = self.searchFiles(name)
            for pkg in fileresults:
                result[pkg.pkgid] = pkg
        
        result = result.values()
        self._cache[prcotype][name] = result

        if self.auto_close:
            self.ts.close()

        return result

    def searchProvides(self, name):
        return self.searchPrco(name, 'provides')

    def searchRequires(self, name):
        return self.searchPrco(name, 'requires')

    def searchObsoletes(self, name):
        return self.searchPrco(name, 'obsoletes')

    def searchConflicts(self, name):
        return self.searchPrco(name, 'conflicts')

    def simplePkgList(self):
        return self.pkglist

    installed = PackageSackBase.contains

    def returnNewestByNameArch(self, naTup=None, patterns=None):

        #FIXME - should this (or any packagesack) be returning tuples?
        if not naTup:
            return
        
        (name, arch) = naTup

        allpkg = self._search(name=name, arch=arch)

        if not allpkg:
            raise Errors.PackageSackError, 'No Package Matching %s' % name

        return [ po.pkgtup for po in misc.newestInList(allpkg) ]

    def returnNewestByName(self, name=None):
        if not name:
            return

        allpkgs = self._search(name=name)

        if not allpkgs:
            raise Errors.PackageSackError, 'No Package Matching %s' % name

        return misc.newestInList(allpkgs)

    @staticmethod
    def _compile_patterns(patterns, ignore_case=False):
        if not patterns or len(patterns) > constants.PATTERNS_MAX:
            return None
        ret = []
        for pat in patterns:
            if not pat:
                continue
            qpat = pat[0]
            if qpat in ('?', '*'):
                qpat = None
            if ignore_case:
                if qpat is not None:
                    qpat = qpat.lower()
                ret.append((qpat, re.compile(fnmatch.translate(pat), re.I)))
            else:
                ret.append((qpat, re.compile(fnmatch.translate(pat))))
        return ret
    @staticmethod
    def _match_repattern(repatterns, hdr, ignore_case):
        if repatterns is None:
            return True

        for qpat, repat in repatterns:
            epoch = hdr['epoch']
            if epoch is None:
                epoch = '0'
            else:
                epoch = str(epoch)
            qname = hdr['name'][0]
            if ignore_case:
                qname = qname.lower()
            if qpat is not None and qpat != qname and qpat != epoch[0]:
                continue
            if repat.match(hdr['name']):
                return True
            if repat.match("%(name)s-%(version)s-%(release)s.%(arch)s" % hdr):
                return True
            if repat.match("%(name)s.%(arch)s" % hdr):
                return True
            if repat.match("%(name)s-%(version)s" % hdr):
                return True
            if repat.match("%(name)s-%(version)s-%(release)s" % hdr):
                return True
            if repat.match(epoch + ":%(name)s-%(version)s-%(release)s.%(arch)s"
                           % hdr):
                return True
            if repat.match("%(name)s-%(epoch)s:%(version)s-%(release)s.%(arch)s"
                           % hdr):
                return True
        return False

    def returnPackages(self, repoid=None, patterns=None, ignore_case=False):
        """Returns a list of packages. Note that the packages are
           always filtered to those matching the patterns/case. repoid is
           ignored, and is just here for compatibility with non-rpmdb sacks. """
        if not self._completely_loaded:
            rpats = self._compile_patterns(patterns, ignore_case)
            for hdr, idx in self._all_packages():
                if self._match_repattern(rpats, hdr, ignore_case):
                    self._makePackageObject(hdr, idx)
            self._completely_loaded = patterns is None

        pkgobjlist = self._idx2pkg.values()
        # Remove gpg-pubkeys, as no sane callers expects/likes them...
        if self._loaded_gpg_keys:
            pkgobjlist = [pkg for pkg in pkgobjlist if pkg.name != 'gpg-pubkey']
        if patterns:
            pkgobjlist = parsePackages(pkgobjlist, patterns, not ignore_case)
            pkgobjlist = pkgobjlist[0] + pkgobjlist[1]
        return pkgobjlist

    def _uncached_returnConflictPackages(self):
        if self._cached_conflicts_data is None:
            ret = []
            for pkg in self.returnPackages():
                if len(pkg.conflicts):
                    ret.append(pkg)
            self._cached_conflicts_data = ret
        return self._cached_conflicts_data

    def _write_conflicts_new(self, pkgs, rpmdbv):
        if not os.access(self._cachedir, os.W_OK):
            return

        conflicts_fname = self._cachedir + '/conflicts'
        fo = open(conflicts_fname + '.tmp', 'w')
        fo.write("%s\n" % rpmdbv)
        fo.write("%u\n" % len(pkgs))
        for pkg in sorted(pkgs):
            for var in pkg.pkgtup:
                fo.write("%s\n" % var)
        fo.close()
        os.rename(conflicts_fname + '.tmp', conflicts_fname)

    def _write_conflicts(self, pkgs):
        rpmdbv = self.simpleVersion(main_only=True)[0]
        self._write_conflicts_new(pkgs, rpmdbv)

    def _read_conflicts(self):
        if not self.__cache_rpmdb__:
            return None

        def _read_str(fo):
            return fo.readline()[:-1]

        conflict_fname = self._cachedir + '/conflicts'
        if not os.path.exists(conflict_fname):
            return None

        fo = open(conflict_fname)
        frpmdbv = fo.readline()
        rpmdbv = self.simpleVersion(main_only=True)[0]
        if not frpmdbv or rpmdbv != frpmdbv[:-1]:
            return None

        ret = []
        try:
            # Read the conflicts...
            pkgtups_num = int(_read_str(fo))
            while pkgtups_num > 0:
                pkgtups_num -= 1

                # n, a, e, v, r
                pkgtup = (_read_str(fo), _read_str(fo),
                          _read_str(fo), _read_str(fo), _read_str(fo))
                int(pkgtup[2]) # Check epoch is valid
                ret.extend(self.searchPkgTuple(pkgtup))
            if fo.readline() != '': # Should be EOF
                return None
        except ValueError:
            return None

        self._cached_conflicts_data = ret
        return self._cached_conflicts_data

    def transactionCacheConflictPackages(self, pkgs):
        if self.__cache_rpmdb__:
            self._trans_cache_store['conflicts'] = pkgs

    def returnConflictPackages(self):
        """ Return a list of packages that have conflicts. """
        pkgs = self._read_conflicts()
        if pkgs is None:
            pkgs = self._uncached_returnConflictPackages()
            if self.__cache_rpmdb__:
                self._write_conflicts(pkgs)

        return pkgs

    def transactionResultVersion(self, rpmdbv):
        """ We are going to do a transaction, and the parameter will be the
            rpmdb version when we finish. The idea being we can update all
            our rpmdb caches for that rpmdb version. """

        if not self.__cache_rpmdb__:
            self._trans_cache_store = {}
            return

        if 'conflicts' in self._trans_cache_store:
            pkgs = self._trans_cache_store['conflicts']
            self._write_conflicts_new(pkgs, rpmdbv)

        if 'file-requires' in self._trans_cache_store:
            data = self._trans_cache_store['file-requires']
            self._write_file_requires(rpmdbv, data)

        self._trans_cache_store = {}

    def transactionReset(self):
        """ We are going to reset the transaction, because the data we've added
            already might now be invalid (Eg. skip-broken, or splitting a
            transaction). """

        self._trans_cache_store = {}

    def returnGPGPubkeyPackages(self):
        """ Return packages of the gpg-pubkeys ... hacky. """
        ts = self.readOnlyTS()
        mi = ts.dbMatch('name', 'gpg-pubkey')
        ret = []
        for hdr in mi:
            self._loaded_gpg_keys = True
            ret.append(self._makePackageObject(hdr, mi.instance()))
        return ret

    def _read_file_requires(self):
        def _read_str(fo):
            return fo.readline()[:-1]

        assert self.__cache_rpmdb__
        if not os.path.exists(self._cachedir + '/file-requires'):
            return None, None

        rpmdbv = self.simpleVersion(main_only=True)[0]
        fo = open(self._cachedir + '/file-requires')
        frpmdbv = fo.readline()
        if not frpmdbv or rpmdbv != frpmdbv[:-1]:
            return None, None

        iFR = {}
        iFP = {}
        try:
            # Read the requires...
            pkgtups_num = int(_read_str(fo))
            while pkgtups_num > 0:
                pkgtups_num -= 1

                # n, a, e, v, r
                pkgtup = (_read_str(fo), _read_str(fo),
                          _read_str(fo), _read_str(fo), _read_str(fo))
                int(pkgtup[2]) # Check epoch is valid

                files_num = int(_read_str(fo))
                while files_num > 0:
                    files_num -= 1

                    fname = _read_str(fo)

                    iFR.setdefault(pkgtup, []).append(fname)

            # Read the provides...
            files_num = int(_read_str(fo))
            while files_num > 0:
                files_num -= 1
                fname = _read_str(fo)
                pkgtups_num = int(_read_str(fo))
                while pkgtups_num > 0:
                    pkgtups_num -= 1

                    # n, a, e, v, r
                    pkgtup = (_read_str(fo), _read_str(fo),
                              _read_str(fo), _read_str(fo), _read_str(fo))
                    int(pkgtup[2]) # Check epoch is valid

                    iFP.setdefault(fname, []).append(pkgtup)

            if fo.readline() != '': # Should be EOF
                return None, None
        except ValueError:
            return None, None

        return iFR, iFP

    def fileRequiresData(self):
        """ Get a cached copy of the fileRequiresData for
            depsolving/checkFileRequires, note the giant comment in that
            function about how we don't keep this perfect for the providers of
            the requires. """
        if self.__cache_rpmdb__:
            iFR, iFP = self._read_file_requires()
            if iFR is not None:
                return iFR, set(), iFP

        installedFileRequires = {}
        installedUnresolvedFileRequires = set()
        resolved = set()
        for pkg in self.returnPackages():
            for name, flag, evr in pkg.requires:
                if not name.startswith('/'):
                    continue
                installedFileRequires.setdefault(pkg.pkgtup, []).append(name)
                if name not in resolved:
                    dep = self.getProvides(name, flag, evr)
                    resolved.add(name)
                    if not dep:
                        installedUnresolvedFileRequires.add(name)

        fileRequires = set()
        for fnames in installedFileRequires.itervalues():
            fileRequires.update(fnames)
        installedFileProviders = {}
        for fname in fileRequires:
            pkgtups = [pkg.pkgtup for pkg in self.getProvides(fname)]
            installedFileProviders[fname] = pkgtups

        ret =  (installedFileRequires, installedUnresolvedFileRequires,
                installedFileProviders)
        if self.__cache_rpmdb__:
            rpmdbv = self.simpleVersion(main_only=True)[0]
            self._write_file_requires(rpmdbv, ret)

        return ret

    def transactionCacheFileRequires(self, installedFileRequires,
                                     installedUnresolvedFileRequires,
                                     installedFileProvides,
                                     problems):
        if not self.__cache_rpmdb__:
            return

        if installedUnresolvedFileRequires or problems:
            return

        data = (installedFileRequires,
                installedUnresolvedFileRequires,
                installedFileProvides)

        self._trans_cache_store['file-requires'] = data

    def _write_file_requires(self, rpmdbversion, data):
        if not os.access(self._cachedir, os.W_OK):
            return

        (installedFileRequires,
         installedUnresolvedFileRequires,
         installedFileProvides) = data

        fo = open(self._cachedir + '/file-requires.tmp', 'w')
        fo.write("%s\n" % rpmdbversion)

        fo.write("%u\n" % len(installedFileRequires))
        for pkgtup in sorted(installedFileRequires):
            for var in pkgtup:
                fo.write("%s\n" % var)
            filenames = set(installedFileRequires[pkgtup])
            fo.write("%u\n" % len(filenames))
            for fname in sorted(filenames):
                fo.write("%s\n" % fname)

        fo.write("%u\n" % len(installedFileProvides))
        for fname in sorted(installedFileProvides):
            fo.write("%s\n" % fname)

            pkgtups = set(installedFileProvides[fname])
            fo.write("%u\n" % len(pkgtups))
            for pkgtup in sorted(pkgtups):
                for var in pkgtup:
                    fo.write("%s\n" % var)
        fo.close()
        os.rename(self._cachedir + '/file-requires.tmp',
                  self._cachedir + '/file-requires')

    def _get_cached_simpleVersion_main(self):
        """ Return the cached string of the main rpmdbv. """
        if self._have_cached_rpmdbv_data is not None:
            return self._have_cached_rpmdbv_data

        if not self.__cache_rpmdb__:
            return None

        #  This test is "obvious" and the only thing to come out of:
        # http://lists.rpm.org/pipermail/rpm-maint/2007-November/001719.html
        # ...if anything gets implemented, we should change.
        rpmdbvfname = self._cachedir + "/version"
        rpmdbfname  = self.root + "/var/lib/rpm/Packages"

        if os.path.exists(rpmdbvfname) and os.path.exists(rpmdbfname):
            # See if rpmdb has "changed" ...
            nmtime = os.path.getmtime(rpmdbvfname)
            omtime = os.path.getmtime(rpmdbfname)
            if omtime <= nmtime:
                rpmdbv = open(rpmdbvfname).readline()[:-1]
                self._have_cached_rpmdbv_data  = rpmdbv
        return self._have_cached_rpmdbv_data

    def _put_cached_simpleVersion_main(self, rpmdbv):
        self._have_cached_rpmdbv_data  = str(rpmdbv)

        if not self.__cache_rpmdb__:
            return

        rpmdbvfname = self._cachedir + "/version"
        if not os.access(self._cachedir, os.W_OK):
            if os.path.exists(self._cachedir):
                return

            try:
                os.makedirs(self._cachedir)
            except (IOError, OSError), e:
                return

        fo = open(rpmdbvfname + ".tmp", "w")
        fo.write(self._have_cached_rpmdbv_data)
        fo.write('\n')
        fo.close()
        os.rename(rpmdbvfname + ".tmp", rpmdbvfname)

    def simpleVersion(self, main_only=False, groups={}):
        """ Return a simple version for all installed packages. """
        def _up_revs(irepos, repoid, rev, pkg, csum):
            irevs = irepos.setdefault(repoid, {})
            rpsv = irevs.setdefault(None, PackageSackVersion())
            rpsv.update(pkg, csum)
            if rev is not None:
                rpsv = irevs.setdefault(rev, PackageSackVersion())
                rpsv.update(pkg, csum)

        if main_only and not groups:
            rpmdbv = self._get_cached_simpleVersion_main()
            if rpmdbv is not None:
                return [rpmdbv, {}]

        main = PackageSackVersion()
        irepos = {}
        main_grps = {}
        irepos_grps = {}
        for pkg in sorted(self.returnPackages()):
            ydbi = pkg.yumdb_info
            csum = None
            if 'checksum_type' in ydbi and 'checksum_data' in ydbi:
                csum = (ydbi.checksum_type, ydbi.checksum_data)
            main.update(pkg, csum)

            for group in groups:
                if pkg.name in groups[group]:
                    if group not in main_grps:
                        main_grps[group] = PackageSackVersion()
                        irepos_grps[group] = {}
                    main_grps[group].update(pkg, csum)

            if main_only:
                continue

            repoid = 'installed'
            rev = None
            if 'from_repo' in pkg.yumdb_info:
                repoid = '@' + pkg.yumdb_info.from_repo
                if 'from_repo_revision' in pkg.yumdb_info:
                    rev = pkg.yumdb_info.from_repo_revision

            _up_revs(irepos, repoid, rev, pkg, csum)
            for group in groups:
                if pkg.name in groups[group]:
                    _up_revs(irepos_grps[group], repoid, rev, pkg, csum)

        if self._have_cached_rpmdbv_data is None:
            self._put_cached_simpleVersion_main(main)

        if groups:
            return [main, irepos, main_grps, irepos_grps]
        return [main, irepos]

    @staticmethod
    def _find_search_fields(fields, searchstrings, hdr):
        count = 0
        for s in searchstrings:
            for field in fields:
                value = to_unicode(hdr[field])
                if value and value.lower().find(s) != -1:
                    count += 1
                    break
        return count

    def searchPrimaryFieldsMultipleStrings(self, fields, searchstrings,
                                           lowered=False):
        if not lowered:
            searchstrings = map(lambda x: x.lower(), searchstrings)
        ret = []
        for hdr, idx in self._all_packages():
            n = self._find_search_fields(fields, searchstrings, hdr)
            if n > 0:
                ret.append((self._makePackageObject(hdr, idx), n))
        return ret
    def searchNames(self, names=[]):
        returnList = []
        for name in names:
            returnList.extend(self._search(name=name))
        return returnList

    def searchNevra(self, name=None, epoch=None, ver=None, rel=None, arch=None):
        return self._search(name, epoch, ver, rel, arch)

    def excludeArchs(self, archlist):
        pass
    
    def returnLeafNodes(self, repoid=None):
        ts = self.readOnlyTS()
        return [ self._makePackageObject(h, mi) for (h, mi) in ts.returnLeafNodes(headers=True) ]
        
    # Helper functions
    def _all_packages(self):
        '''Generator that yield (header, index) for all packages
        '''
        ts = self.readOnlyTS()
        mi = ts.dbMatch()

        for hdr in mi:
            if hdr['name'] != 'gpg-pubkey':
                yield (hdr, mi.instance())
        del mi
        if self.auto_close:
            self.ts.close()


    def _header_from_index(self, idx):
        """returns a package header having been given an index"""
        warnings.warn('_header_from_index() will go away in a future version of Yum.\n',
                Errors.YumFutureDeprecationWarning, stacklevel=2)

        ts = self.readOnlyTS()
        try:
            mi = ts.dbMatch(0, idx)
        except (TypeError, StopIteration), e:
            #FIXME: raise some kind of error here
            print 'No index matching %s found in rpmdb, this is bad' % idx
            yield None # it should REALLY not be returning none - this needs to be right
        else:
            hdr = mi.next()
            yield hdr
            del hdr

        del mi
        if self.auto_close:
            self.ts.close()


    def _search(self, name=None, epoch=None, ver=None, rel=None, arch=None):
        '''List of matching packages, to zero or more of NEVRA.'''
        pkgtup = (name, arch, epoch, ver, rel)
        if pkgtup in self._tup2pkg:
            return [self._tup2pkg[pkgtup]]

        loc = locals()
        ret = []

        if self._completely_loaded:
            if name is not None:
                pkgs = self._name2pkg.get(name, [])
            else:
                pkgs = self.returnPkgs()
            for po in pkgs:
                for tag in ('arch', 'rel', 'ver', 'epoch'):
                    if loc[tag] is not None and loc[tag] != getattr(po, tag):
                        break
                else:
                    ret.append(po)
            return ret

        ts = self.readOnlyTS()
        if name is not None:
            mi = ts.dbMatch('name', name)
        elif arch is not None:
            mi = ts.dbMatch('arch', arch)
        else:
            mi = ts.dbMatch()
            self._completely_loaded = True

        for hdr in mi:
            if hdr['name'] == 'gpg-pubkey':
                continue
            po = self._makePackageObject(hdr, mi.instance())
            for tag in ('arch', 'rel', 'ver', 'epoch'):
                if loc[tag] is not None and loc[tag] != getattr(po, tag):
                    break
            else:
                ret.append(po)

        if self.auto_close:
            self.ts.close()

        return ret

    def _makePackageObject(self, hdr, index):
        if index in self._idx2pkg:
            return self._idx2pkg[index]
        po = RPMInstalledPackage(hdr, index, self)
        self._idx2pkg[index] = po
        self._name2pkg.setdefault(po.name, []).append(po)
        self._tup2pkg[po.pkgtup] = po
        return po
        
    def _hdr2pkgTuple(self, hdr):
        name = misc.share_data(hdr['name'])
        arch = misc.share_data(hdr['arch'])
         # convert these to strings to be sure
        ver = misc.share_data(str(hdr['version']))
        rel = misc.share_data(str(hdr['release']))
        epoch = hdr['epoch']
        if epoch is None:
            epoch = '0'
        else:
            epoch = str(epoch)
        epoch = misc.share_data(epoch)
        return misc.share_data((name, arch, epoch, ver, rel))

    # deprecated options for compat only - remove once rpmdb is converted:
    def getPkgList(self):
        warnings.warn('getPkgList() will go away in a future version of Yum.\n'
                'Please access this via the pkglist attribute.',
                DeprecationWarning, stacklevel=2)
    
        return self.pkglist

    def getHdrList(self):
        warnings.warn('getHdrList() will go away in a future version of Yum.\n',
                DeprecationWarning, stacklevel=2)
        return [ hdr for hdr, idx in self._all_packages() ]

    def getNameArchPkgList(self):
        warnings.warn('getNameArchPkgList() will go away in a future version of Yum.\n',
                DeprecationWarning, stacklevel=2)
        
        lst = []
        for (name, arch, epoch, ver, rel) in self.pkglist:
            lst.append((name, arch))
        
        return miscutils.unique(lst)
        
    def getNamePkgList(self):
        warnings.warn('getNamePkgList() will go away in a future version of Yum.\n',
                DeprecationWarning, stacklevel=2)
    
        lst = []
        for (name, arch, epoch, ver, rel) in self.pkglist:
            lst.append(name)

        return miscutils.unique(lst)
    
    def returnTupleByKeyword(self, name=None, arch=None, epoch=None, ver=None, rel=None):
        warnings.warn('returnTuplebyKeyword() will go away in a future version of Yum.\n',
                DeprecationWarning, stacklevel=2)
        return [po.pkgtup for po in self._search(name=name, arch=arch, epoch=epoch, ver=ver, rel=rel)]

    def returnHeaderByTuple(self, pkgtuple):
        warnings.warn('returnHeaderByTuple() will go away in a future version of Yum.\n',
                DeprecationWarning, stacklevel=2)
        """returns a list of header(s) based on the pkgtuple provided"""
        
        (n, a, e, v, r) = pkgtuple
        
        lst = self.searchNevra(name=n, arch=a, epoch=e, ver=v, rel=r)
        if len(lst) > 0:
            item = lst[0]
            return [item.hdr]
        else:
            return []

    def returnIndexByTuple(self, pkgtuple):
        """returns a list of header indexes based on the pkgtuple provided"""

        warnings.warn('returnIndexbyTuple() will go away in a future version of Yum.\n',
                DeprecationWarning, stacklevel=2)

        name, arch, epoch, version, release = pkgtuple

        # Normalise epoch
        if epoch in (None, 0, '(none)', ''):
            epoch = '0'

        return [po.idx for po in self._search(name, epoch, version, release, arch)]
        
    def addDB(self, ts):
        # Can't support this now
        raise NotImplementedError

    @staticmethod
    def _genDeptup(name, flags, version):
        """ Given random stuff, generate a usable dep tuple. """

        if flags == 0:
            flags = None

        if type(version) is types.StringType:
            (r_e, r_v, r_r) = miscutils.stringToVersion(version)
        # would this ever be a ListType?
        elif type(version) in (types.TupleType, types.ListType):
            (r_e, r_v, r_r) = version
        else:
            # FIXME: This isn't always  type(version) is types.NoneType:
            # ...not sure what it is though, come back to this
            r_e = r_v = r_r = None

        deptup = (name, misc.share_data(flags),
                  (misc.share_data(r_e), misc.share_data(r_v),
                   misc.share_data(r_r)))
        return misc.share_data(deptup)

    def getProvides(self, name, flags=None, version=(None, None, None)):
        """searches the rpmdb for what provides the arguments
           returns a list of pkg objects of providing packages, possibly empty"""

        name = misc.share_data(name)
        deptup = self._genDeptup(name, flags, version)
        if deptup in self._get_pro_cache:
            return self._get_pro_cache[deptup]
        r_v = deptup[2][1]
        
        pkgs = self.searchProvides(name)
        
        result = { }
        
        for po in pkgs:
            if name[0] == '/' and r_v is None:
                result[po] = [(name, None, (None, None, None))]
                continue
            hits = po.matchingPrcos('provides', deptup)
            if hits:
                result[po] = hits
        self._get_pro_cache[deptup] = result
        return result

    def whatProvides(self, name, flags, version):
        # XXX deprecate?
        return [po.pkgtup for po in self.getProvides(name, flags, version)]

    def getRequires(self, name, flags=None, version=(None, None, None)):
        """searches the rpmdb for what provides the arguments
           returns a list of pkgtuples of providing packages, possibly empty"""

        name = misc.share_data(name)
        deptup = self._genDeptup(name, flags, version)
        if deptup in self._get_req_cache:
            return self._get_req_cache[deptup]
        r_v = deptup[2][1]

        pkgs = self.searchRequires(name)

        result = { }

        for po in pkgs:
            if name[0] == '/' and r_v is None:
                # file dep add all matches to the defSack
                result[po] = [(name, None, (None, None, None))]
                continue
            hits = po.matchingPrcos('requires', deptup)
            if hits:
                result[po] = hits
        self._get_req_cache[deptup] = result
        return result

    def whatRequires(self, name, flags, version):
        # XXX deprecate?
        return [po.pkgtup for po in self.getRequires(name, flags, version)]

    def return_running_packages(self):
        """returns a list of yum installed package objects which own a file
           that are currently running or in use."""
        pkgs = {}
        for pid in misc.return_running_pids():
            for fn in misc.get_open_files(pid):
                for pkg in self.searchFiles(fn):
                    pkgs[pkg] = 1

        return sorted(pkgs.keys())

    def check_dependencies(self, pkgs=None):
        """ Checks for any missing dependencies. """

        if pkgs is None:
            pkgs = self.returnPackages()

        providers = set() # Speedup, as usual :)
        problems = []
        for pkg in sorted(pkgs): # The sort here is mainly for "UI"
            for rreq in pkg.requires:
                if rreq[0].startswith('rpmlib'): continue
                if rreq in providers:            continue

                (req, flags, ver) = rreq
                if self.getProvides(req, flags, ver):
                    providers.add(rreq)
                    continue
                flags = yum.depsolve.flags.get(flags, flags)
                missing = miscutils.formatRequire(req, ver, flags)
                prob = RPMDBProblemDependency(pkg, "requires", missing=missing)
                problems.append(prob)

            for creq in pkg.conflicts:
                if creq[0].startswith('rpmlib'): continue

                (req, flags, ver) = creq
                res = self.getProvides(req, flags, ver)
                if not res:
                    continue
                flags = yum.depsolve.flags.get(flags, flags)
                found = miscutils.formatRequire(req, ver, flags)
                prob = RPMDBProblemDependency(pkg, "conflicts", found=found,
                                              conflicts=res)
                problems.append(prob)
        return problems

    def _iter_two_pkgs(self, ignore):
        last = None
        for pkg in sorted(self.returnPackages()):
            if pkg.name in ignore:
                continue
            if last is None:
                last = pkg
                continue
            yield last, pkg
            last = pkg

    def check_duplicates(self, ignore=[]):
        """ Checks for any missing dependencies. """

        problems = []
        for last, pkg in self._iter_two_pkgs(ignore):
            if pkg.name != last.name:
                continue
            if pkg.verEQ(last) and pkg != last:
                if arch.isMultiLibArch(pkg.arch) and last.arch != 'noarch':
                    continue
                if arch.isMultiLibArch(last.arch) and pkg.arch != 'noarch':
                    continue

            # More than one pkg, they aren't version equal, or aren't multiarch
            problems.append(RPMDBProblemDuplicate(pkg, duplicate=last))
        return problems


def _sanitize(path):
    return path.replace('/', '').replace('~', '')


class RPMDBAdditionalData(object):
    """class for access to the additional data not able to be stored in the
       rpmdb"""
    # dir: /var/lib/yum/yumdb/
    # pkgs stored in name[0]/name[1]/pkgid-name-ver-rel-arch dirs
    # dirs have files per piece of info we're keeping
    #    repoid, install reason, status, blah, (group installed for?), notes?
    
    def __init__(self, db_path='/var/lib/yum/yumdb'):
        self.conf = misc.GenericHolder()
        self.conf.db_path = db_path
        self.conf.writable = False
        
        self._packages = {} # pkgid = dir
        if not os.path.exists(self.conf.db_path):
            try:
                os.makedirs(self.conf.db_path)
            except (IOError, OSError), e:
                # some sort of useful thing here? A warning?
                return
            self.conf.writable = True
        else:
            if os.access(self.conf.db_path, os.W_OK):
                self.conf.writable = True
                
        # glob the path and get a dict of pkgs to their subdir
        glb = '%s/*/*/' % self.conf.db_path
        pkgdirs = glob.glob(glb)
        for d in pkgdirs:
            pkgid = os.path.basename(d).split('-')[0]
            self._packages[pkgid] = d

    def _get_dir_name(self, pkgtup, pkgid):
        if pkgid in self._packages:
            return self._packages[pkgid]
        (n, a, e, v,r) = pkgtup
        n = _sanitize(n) # Please die in a fire rpmbuild
        thisdir = '%s/%s/%s-%s-%s-%s-%s' % (self.conf.db_path,
                                            n[0], pkgid, n, v, r, a)
        self._packages[pkgid] = thisdir
        return thisdir

    def get_package(self, po=None, pkgtup=None, pkgid=None):
        """Return an RPMDBAdditionalDataPackage Object for this package"""
        if po:
            thisdir = self._get_dir_name(po.pkgtup, po.pkgid)
        elif pkgtup and pkgid:
            thisdir = self._get_dir_name(pkgtup, pkgid)
        else:
            raise ValueError,"Pass something to RPMDBAdditionalData.get_package"
        
        return RPMDBAdditionalDataPackage(self.conf, thisdir)

    def sync_with_rpmdb(self, rpmdbobj):
        """populate out the dirs and remove all the items no longer in the rpmd
           and/or populate various bits to the currently installed version"""
        # TODO:
        # get list of all items in the yumdb
        # remove any no longer in the rpmdb/andor migrate them up to the currently
        # installed version
        # add entries for items in the rpmdb if they don't exist in the yumdb

        pass

class RPMDBAdditionalDataPackage(object):
    def __init__(self, conf, pkgdir):
        self._conf = conf
        self._mydir = pkgdir
        # FIXME needs some intelligent caching beyond the FS cache
        self._read_cached_data = {}

    def _write(self, attr, value):
        # check for self._conf.writable before going on?
        if not os.path.exists(self._mydir):
            os.makedirs(self._mydir)

        attr = _sanitize(attr)
        if attr in self._read_cached_data:
            del self._read_cached_data[attr]
        fn = self._mydir + '/' + attr
        fn = os.path.normpath(fn)
        fo = open(fn + '.tmp', 'w')
        try:
            fo.write(value)
        except (OSError, IOError), e:
            raise AttributeError, "Cannot set attribute %s on %s" % (attr, self)

        fo.flush()
        fo.close()
        del fo
        os.rename(fn +  '.tmp', fn) # even works on ext4 now!:o
        self._read_cached_data[attr] = value
    
    def _read(self, attr):
        attr = _sanitize(attr)

        if attr.endswith('.tmp'):
            raise AttributeError, "%s has no attribute %s" % (self, attr)

        if attr in self._read_cached_data:
            return self._read_cached_data[attr]

        fn = self._mydir + '/' + attr
        if not os.path.exists(fn):
            raise AttributeError, "%s has no attribute %s" % (self, attr)

        fo = open(fn, 'r')
        self._read_cached_data[attr] = fo.read()
        fo.close()
        del fo
        return self._read_cached_data[attr]
    
    def _delete(self, attr):
        """remove the attribute file"""

        attr = _sanitize(attr)
        fn = self._mydir + '/' + attr
        if attr in self._read_cached_data:
            del self._read_cached_data[attr]
        if os.path.exists(fn):
            try:
                os.unlink(fn)
            except (IOError, OSError):
                raise AttributeError, "Cannot delete attribute %s on " % (attr, self)
    
    def __getattr__(self, attr):
        return self._read(attr)

    def __setattr__(self, attr, value):
        if not attr.startswith('_'):
            self._write(attr, value)
        else:
            object.__setattr__(self, attr, value)

    def __delattr__(self, attr):
        if not attr.startswith('_'):
            self._delete(attr)
        else:
            object.__delattr__(self, attr)

    def __iter__(self, show_hidden=False):
        for item in self._read_cached_data:
            yield item
        for item in glob.glob(self._mydir + '/*'):
            item = item[(len(self._mydir) + 1):]
            if item in self._read_cached_data:
                continue
            if not show_hidden and item.endswith('.tmp'):
                continue
            yield item

    def clean(self):
        # purge out everything
        for item in self.__iter__(show_hidden=True):
            self._delete(item)
        try:
            os.rmdir(self._mydir)
        except OSError:
            pass

#    def __dir__(self): # for 2.6 and beyond, apparently
#        return list(self.__iter__()) + self.__dict__.keys()

    def get(self, attr, default=None):
        """retrieve an add'l data obj"""

        try:
            res = self._read(attr)
        except AttributeError:
            return default
        return res
        
        
def main():
    sack = RPMDBPackageSack('/')
    for p in sack.simplePkgList():
        print p

if __name__ == '__main__':
    main()

