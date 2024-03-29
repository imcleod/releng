#!/usr/bin/python
#
# find_unblocked_orphans.py - A utility to find orphaned packages in pkgdb
#                             that are unblocked in koji and to show what
#                             may require those orphans
#
# Copyright (c) 2009-2013 Red Hat
# SPDX-License-Identifier:	GPL-2.0
#
# Authors:
#     Jesse Keating <jkeating@redhat.com>
#     Till Maas <opensource@till.name>

from collections import OrderedDict
import cPickle as pickle
import datetime
import os
import argparse
from Queue import Queue
import sys
from threading import Thread

import koji
import pkgdb2client
import yum

try:
    import texttable
    with_texttable = True
except ImportError:
    with_texttable = False

# Set some variables
# Some of these could arguably be passed in as args.
# If this is pre-branch, these repos should be rawhide; otherwise,
# they should be branched.
DEFAULT_REPO = 'http://kojipkgs.fedoraproject.org/mash/rawhide/i386/os'
DEFAULT_SOURCE_REPO = \
    'http://kojipkgs.fedoraproject.org/mash/rawhide/source/SRPMS'
TAG = 'f21'  # tag to check in koji

# pre-branch, this should be master'. Post-branch, it will be something like
# fXY
RAWHIDE_BRANCHNAME = 'master'  # pkgdb name for the devel branch

# pkgdb uid for orphan
ORPHAN_UID = 'orphan'

HEADER = """The following packages are orphaned or did not build for two
releases and will be retired when Fedora ({0}) is branched, unless someone
adopts them. If you know for sure that the package should be retired, please do
so now with a proper reason:
https://fedoraproject.org/wiki/How_to_remove_a_package_at_end_of_life

According to https://fedoraproject.org/wiki/Schedule branching will
occur not earlier than 2014-07-08. The packages will be retired shortly before.

Note: If you received this mail directly you (co)maintain one of the affected
packages or a package that depends on one.
""".format(TAG.upper())

FOOTER = """The script creating this output is run and developed by Fedora
Release Engineering. Please report issues at its trac instance:
https://fedorahosted.org/rel-eng/
The sources of this script can be found at:
https://git.fedorahosted.org/cgit/releng/tree/scripts/find_unblocked_orphans.py
"""
pkgdb = pkgdb2client.PkgDB()


def get_cache(filename,
              max_age=3600,
              cachedir='~/.cache',
              default=None):
    """ Get a pickle file from cache
    :param filename: Filename with pickle data
    :type filename: str
    :param max_age: Maximum age of cache in seconds
    :type max_age: int
    :param cachedir: Directory to get file from
    :type cachedir: str
    :param default: Default value if cache is too old or does not exist
    :type default: object
    :returns: pickled object
    """
    cache_file = os.path.expanduser(os.path.join(cachedir, filename))
    try:
        with open(cache_file, "rb") as pickle_file:
            mtime = os.fstat(pickle_file.fileno()).st_mtime
            mtime = datetime.datetime.fromtimestamp(mtime)
            now = datetime.datetime.now()
            if (now - mtime).total_seconds() < max_age:
                res = pickle.load(pickle_file)
            else:
                res = default
    except IOError:
        res = default
    return res


def write_cache(data, filename, cachedir='~/.cache'):
    """ Write ``data`` do pickle cache
    :param data: Data to cache
    :param filename: Filename for cache file
    :type filename: str
    :param cachedir: Dir for cachefile
    :type cachedir: str
    """
    cache_file = os.path.expanduser(os.path.join(cachedir, filename))
    with open(cache_file, "wb") as pickle_file:
        pickle.dump(data, pickle_file)


people_queue = Queue()
people_dict = get_cache("orphans-people.pickle", default={})


def get_people(package, branch=RAWHIDE_BRANCHNAME):
    def associated(pkginfo, exclude=None):
        """

        :param exclude: People to exclude, e.g. the point of contact.
        :type exclude: list
        """
        other_people = set()
        for acl in pkginfo.get("acls", []):
            if acl["status"] == "Approved":
                fas_name = acl["fas_name"]
                if fas_name != "group::provenpackager" and \
                        fas_name not in exclude:
                    other_people.add(fas_name)
        return sorted(other_people)

    pkginfo = pkgdb.get_package(package, branches=branch)
    pkginfo = pkginfo["packages"][0]
    people_ = [pkginfo["point_of_contact"]]
    people_.extend(associated(pkginfo, exclude=people_))
    return people_


def people_worker():
    while True:
        package = people_queue.get()
        if package not in people_dict:
            people_ = get_people(package)
            people_dict[package] = people_
        people_queue.task_done()


def setup_yum(repo=DEFAULT_REPO, source_repo=DEFAULT_SOURCE_REPO):
    """ Setup YumBase with two repos
    This code was mostly stolen from
    http://yum.baseurl.org/wiki/YumCodeSnippet/SetupArbitraryRepo
    """

    yb = yum.YumBase()
    yb.preconf.init_plugins = False
    # FIXME: Maybe reuse should be False here
    if not yb.setCacheDir(force=True, reuse=True):
        print >> sys.stderr, "Can't create a tmp. cachedir. "
        sys.exit(1)

    yb.conf.cache = 0

    yb.repos.disableRepo('*')
    yb.add_enable_repo('repo', [repo])
    yb.add_enable_repo('repo-source', [source_repo])
    yb.arch.archlist.append('src')
    return yb

sys.stderr.write("Setting up yum...")
yb = setup_yum()
sys.stderr.write("done\n")


# This function was stolen from pungi
def SRPM(package):
    """Given a package object, get a package object for the
    corresponding source rpm. Requires yum still configured
    and a valid package object."""
    srpm = package.sourcerpm.split('.src.rpm')[0]
    (sname, sver, srel) = srpm.rsplit('-', 2)
    try:
        srpmpo = yb.pkgSack.searchNevra(name=sname,
                                        ver=sver,
                                        rel=srel,
                                        arch='src')[0]
        return srpmpo
    except IndexError:
        print >> sys.stderr, "Error: Cannot find a source rpm for %s" % srpm
        sys.exit(1)


def orphan_packages(cache_filename='orphans.pickle'):
    orphans = get_cache(cache_filename, default={})

    if orphans:
        return orphans
    else:
        pkgdbresponse = pkgdb.get_packages("", orphaned=True,
                                           branches="master", page="all")
        pkgs = pkgdbresponse["packages"]
        for p in pkgs:
            orphans[p["name"]] = p
        try:
            write_cache(orphans, cache_filename)
        except IOError, e:
            sys.stderr.write("Caching of orphans failed: {0}\n".format(e))
        return orphans


def unblocked_packages(packages, tagID=TAG):
    unblocked = []
    kojisession = koji.ClientSession('https://koji.fedoraproject.org/kojihub')

    kojisession.multicall = True
    for p in packages:
        kojisession.listPackages(tagID=tagID, pkgID=p, inherited=True)
    listings = kojisession.multiCall()

    # Check the listings for unblocked packages.

    for pkgname, result in zip(packages, listings):
        if isinstance(result, list):
            [pkg] = result
            if not pkg[0]['blocked']:
                package_name = pkg[0]['package_name']
                people_queue.put(package_name)
                unblocked.append(package_name)
        else:
            print "ERROR: {pkgname}: {error}".format(
                pkgname=pkgname, error=result)
    return unblocked


class BinSrcMapper(object):
    def __init__(self):
        self._src_by_bin = None
        self._bin_by_src = None

    def create_mapping(self):
        src_by_bin = {}  # Dict of source pkg objects by binary package objects
        bin_by_src = {}  # Dict of binary pkgobjects by srpm name
        all_packages = yb.pkgSack.returnPackages()

        # Populate the dicts
        for rpm_package in all_packages:
            if rpm_package.arch == 'src':
                continue
            srpm = SRPM(rpm_package)
            src_by_bin[rpm_package] = srpm
            if srpm.name in bin_by_src:
                bin_by_src[srpm.name].append(rpm_package)
            else:
                bin_by_src[srpm.name] = [rpm_package]

        self._src_by_bin = src_by_bin
        self._bin_by_src = bin_by_src

    @property
    def by_src(self):
        if not self._bin_by_src:
            self.create_mapping()
        return self._bin_by_src

    @property
    def by_bin(self):
        if not self._src_by_bin:
            self.create_mapping()
        return self._src_by_bin

sys.stderr.write("Setting up packager mapper...")
package_mapper = BinSrcMapper()
sys.stderr.write("done\n")


def find_dependent_packages(srpmname, ignore):
    """ Return packages depending on packages built from SRPM ``srpmname``
        that are built from different SRPMS not specified in ``ignore``.

        :param ignore: list of SRPMs of packages that will not be returned as
            dependent packages.
        :type ignore: list() of str()

        :returns: OrderedDict dependent_package: list of requires only provided
                  by package ``srpmname``
                  {dep_pkg: [prov, ...]}
    """
    # Some of this code was stolen from repoquery
    dependent_packages = {}

    # Handle packags not found in the repo
    try:
        rpms = package_mapper.by_src[srpmname]
    except KeyError:
        # If we don't have a package in the repo, there is nothing to do
        sys.stderr.write("Package {0} not found in repo\n".format(srpmname))
        rpms = []

    # provides of all packages built from ``srpmname``
    provides = []
    for pkg in rpms:
        # add all the provides from the package as strings
        string_provides = [yum.misc.prco_tuple_to_string(prov)
                           for prov in pkg.provides]
        provides.extend(string_provides)

        # add all files as provides
        # pkg.files is a dict with keys like "file" and "dir"
        # values are a list of file/dir paths
        for paths in pkg.files.itervalues():
            # sometimes paths start with "//" instead of "/"
            # normalise "//" to "/":
            # os.path.normpath("//") == "//", but
            # os.path.normpath("///") == "/"
            file_provides = [os.path.normpath('//%s' % fn)
                             for fn in paths]
            provides.extend(file_provides)

    # Zip through the provides and find what's needed
    for prov in provides:
        # check only base provide, ignore specific versions
        # "foo = 1.fc20" -> "foo"
        base_provide = prov.split()[0]

        # Elide provide if also provided by another package
        for pkg in yb.pkgSack.searchProvides(base_provide):
            # FIXME: might miss broken dependencies in case the other provider
            # depends on a to-be-removed package as well
            if pkg.sourcerpm.rsplit('-', 2)[0] not in ignore:
                break
        else:
            for dependent_pkg in yb.pkgSack.searchRequires(base_provide):
                # skip if the dependent rpm package belongs to the
                # to-be-removed Fedora package
                if dependent_pkg in package_mapper.by_src[srpmname]:
                    continue

                # skip if the dependent rpm package is also a
                # package that should be removed
                if dependent_pkg.name in ignore:
                    continue

                # use setdefault to either create an entry for the
                # dependent package or add the required prov
                dependent_packages.setdefault(dependent_pkg, set()).add(prov)
    return OrderedDict(sorted(dependent_packages.items()))


def recursive_deps(packages, max_deps=20):
    # get a list of all rpm_pkgs that are to be removed
    rpm_pkg_names = []
    for name in packages:
        # Empty list if pkg is only for a different arch
        bin_pkgs = package_mapper.by_src.get(name, [])
        rpm_pkg_names.extend([p.name for p in bin_pkgs])

    # dict for all dependent package for each to-be-removed package
    dep_map = OrderedDict()
    for name in packages:
        ignore = rpm_pkg_names
        dep_map[name] = OrderedDict()
        to_check = [name]
        allow_more = True
        seen = []
        while True:
            sys.stderr.write("to_check: {0}\n".format(repr(to_check)))
            check_next = to_check.pop()
            seen.append(check_next)
            dependent_packages = find_dependent_packages(check_next, ignore)
            if dependent_packages:
                new_names = []
                new_srpm_names = set()
                for pkg, dependencies in dependent_packages.items():
                    if pkg.arch != "src":
                        srpm_name = package_mapper.by_bin[pkg].name
                    else:
                        srpm_name = pkg.name
                    if srpm_name not in to_check and \
                            srpm_name not in new_names and \
                            srpm_name not in seen:
                        new_names.append(srpm_name)
                    new_srpm_names.add(srpm_name)

                    for dep in dependencies:
                        dep_map[name].setdefault(
                            srpm_name,
                            OrderedDict()
                        ).setdefault(pkg, set()).add(dep)

                for srpm_name in new_srpm_names:
                    people_queue.put(srpm_name)

                ignore.extend(new_names)
                if allow_more:
                    to_check.extend(new_names)
                    dep_count = len(set(dep_map[name].keys() + to_check))
                    if dep_count > max_deps:
                        allow_more = False
                        to_check = to_check[0:max_deps]
            if not to_check:
                break
        if not allow_more:
            sys.stderr.write("More than {0} broken deps for package"
                             "'{1}', dependency check not"
                             " completed\n".format(max_deps, name))
    return dep_map


def maintainer_table(packages):
    affected_people = {}

    if with_texttable:
        table = texttable.Texttable(max_width=80)
        table.header(["Package", "(co)maintainers"])
        table.set_cols_align(["l", "l"])
        table.set_deco(table.HEADER)
    else:
        table = ""

    for package_name in packages:
        people = people_dict[package_name]
        for p in people:
            affected_people.setdefault(p, set()).add(package_name)
        p = ', '.join(people)

        if with_texttable:
            table.add_row([package_name, p])
        else:
            table += "{0} {1}\n".format(package_name, p)

    if with_texttable:
        table = table.draw()
    return table, affected_people


def dependency_info(dep_map, affected_people):
    info = ""
    for package_name, subdict in dep_map.items():
        if subdict:
            info += "Depending on: %s\n" % package_name
            for fedora_package, dependent_packages in subdict.items():
                people = people_dict[fedora_package]
                for p in people:
                    affected_people.setdefault(p, set()).add(package_name)
                p = ", ".join(people)
                info += "\t{0} (maintained by: {1})\n".format(fedora_package,
                                                              p)
                for dep in dependent_packages:
                    provides = ", ".join(sorted(dependent_packages[dep]))
                    info += "\t\t%s requires %s\n" % (dep.nvra, provides)
                info += "\n"
            info += "\n"
    return info


def maintainer_info(affected_people):
    info = ""
    for person in sorted(affected_people.iterkeys()):
        packages = affected_people[person]
        if person == ORPHAN_UID:
            continue
        info += "{0}: {1}\n".format(person, ", ".join(packages))
    return info


def package_info(packages, orphans=None, failed=None):
    info = ""
    sys.stderr.write('Calculating dependencies...')
    # Create yum object and depsolve out if requested.
    # TODO: add app args to either depsolve or not
    dep_map = recursive_deps(packages)
    sys.stderr.write('done\n')

    sys.stderr.write("Waiting for (co)maintainer information...")
    people_queue.join()
    sys.stderr.write("done\n")
    write_cache(people_dict, "orphans-people.pickle")

    table, affected_people = maintainer_table(packages)
    info += table
    info += "\nThe following packages require above mentioned packages:\n"
    info += dependency_info(dep_map, affected_people)

    info += "Affected (co)maintainers\n"
    info += maintainer_info(affected_people)
    if orphans:
        info += "\norphans: " + " ".join(orphans)
        info += "\n"
        orphans_breaking_deps = [o for o in orphans if
                                 o in dep_map and dep_map[o]]
        info += "orphans (depended on): " + " ".join(orphans_breaking_deps)
        info += "\n"
        orphans_not_breaking_deps = [o for o in orphans if
                                     not o in dep_map or not dep_map[o]]
        info += "orphans (not depended on): " + " ".join(
            orphans_not_breaking_deps)
        info += "\n"
    if failed:
        info += "\nFTBFS: " + " ".join(failed)
        info += "\n"
        ftbfs_breaking_deps = [o for o in failed if
                               o in dep_map and dep_map[o]]
        info += "FTBFS (depended on): " + " ".join(ftbfs_breaking_deps)
        info += "\n"
        ftbfs_not_breaking_deps = [o for o in failed if
                                   not o in dep_map or not dep_map[o]]
        info += "FTBFS (not depended on): " + " ".join(
            ftbfs_not_breaking_deps)
        info += "\n"

    addresses = ["{0}@fedoraproject.org".format(p)
                 for p in affected_people.keys() if p != ORPHAN_UID]
    addresses = "Bcc: {0}\n".format(", ".join(addresses))
    return info, addresses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-orphans", dest="skip_orphans",
                        help="Do not look for orphans",
                        default=False, action="store_true")
    parser.add_argument("failed", nargs="*",
                        help="Additional packages, e.g. FTBFS packages")
    args = parser.parse_args()
    failed = args.failed

    if args.skip_orphans:
        orphans = []
    else:
        # list of orphans on the devel branch from pkgdb
        sys.stderr.write('Contacting pkgdb for list of orphans...')
        orphans = orphan_packages()
        sys.stderr.write('done\n')

    # Start threads to get information about (co)maintainers for packages
    for i in range(0, 2):
        people_thread = Thread(target=people_worker)
        people_thread.daemon = True
        people_thread.start()
    # keep pylint silent
    del i

    sys.stderr.write('Getting builds from koji...')
    unblocked = unblocked_packages(sorted(list(set(list(orphans) + failed))))
    sys.stderr.write('done\n')

    print HEADER
    info, addresses = package_info(unblocked, orphans=orphans, failed=failed)
    print info
    print FOOTER

    sys.stderr.write(addresses)

if __name__ == "__main__":
    main()
