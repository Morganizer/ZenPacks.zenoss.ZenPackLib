##############################################################################
#
# Copyright (C) Zenoss, Inc. 2016, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################
import os
import os.path
import sys
import yaml
import collections
import logging
from optparse import OptionGroup

import Globals
from Products.ZenUtils.Utils import unused

from Acquisition import aq_base
from Products.ZenUtils.ZenScriptBase import ZenScriptBase

from ..params.ZenPackSpecParams import ZenPackSpecParams
from ..params.RRDTemplateSpecParams import RRDTemplateSpecParams
from ..params.EventClassSpecParams import EventClassSpecParams
from ..params.EventClassMappingSpecParams import EventClassMappingSpecParams
from ..params.ProcessClassOrganizerSpecParams import ProcessClassOrganizerSpecParams
from ..resources.templates import SETUP_PY
from ..helpers.ZenPackLibLog import ZenPackLibLog, DEFAULTLOG
from ..helpers.loaders import WarningLoader, ZenPackSpecLoader
from ..helpers.Dumper import Dumper
from ..helpers.utils import optimize_yaml, load_yaml_single
from ZenPacks.zenoss.ZenPackLib import zenpacklib
unused(Globals)


class ZPLCommand(ZenScriptBase):
    '''ZPLCommand'''
    LOG = DEFAULTLOG
    version = zenpacklib.__version__

    def __init__(self):
        ZenScriptBase.__init__(self)
        ZenPackLibLog.enable_log_stderr(self.LOG)

    def buildOptions(self):
        ''''''
        ZenScriptBase.buildOptions(self)
        # remove unneeded
        self.parser.remove_option('-C')
        self.parser.remove_option('--genconf')
        self.parser.remove_option('--genxmltable')
        self.parser.remove_option('--genxmlconfigs')
        self.parser.remove_option('--maxlogsize')
        self.parser.remove_option('--maxbackuplogs')
        self.parser.remove_option('--logpath')
        self.parser.usage = "%prog [options] [FILENAME|ZENPACK|DEVICE]"
        self.parser.version = self.version

        group = OptionGroup(self.parser, "ZenPack Conversion")
        group.add_option("-t", "--dump-templates",
                    dest="dump",
                    action="store_true",
                    help="export existing monitoring templates to YAML")
        group.add_option("-e", "--dump-event-classes",
                         dest="dump_event_classes",
                         action="store_true",
                         help="export existing event classes to YAML")
        group.add_option("-r", "--dump-process-classes",
                         dest="dump_process_classes",
                         action="store_true",
                         help="export existing process classes to YAML")
        self.parser.add_option_group(group)

        group = OptionGroup(self.parser, "ZenPack Development")
        group.add_option("-c", "--create",
                    dest="create",
                    action="store_true",
                    help="Create a new ZenPack source directory")
        group.add_option("-l", "--lint",
                    dest="lint",
                    action="store_true",
                    help="check zenpack.yaml syntax for errors")
        group.add_option("-o", "--optimize",
                    dest="optimize",
                    action="store_true",
                    help="optimize zenpack.yaml format and DEFAULTS")
        group.add_option("-d", "--diagram",
                    dest="diagram",
                    action="store_true",
                    help="print YUML (http://yuml.me/) class diagram source based on zenpack.yaml")
        group.add_option("-p", "--paths",
                    dest="paths",
                    action="store_true",
                    help="print possible facet paths for a given device and whether currently filtered.")

        self.parser.add_option_group(group)

    def is_valid_file(self):
        '''Determine if supplied file is valid'''
        errorMessage = ''
        valid = False
        if not os.path.exists(self.options.filename):
            errorMessage = ('WARN: unable to find file {filename}').format(
                filename=self.options.filename,
            )
        else:
            try:
                open(self.options.filename)
                valid = True
            except IOError as e:
                errorMessage = ('WARN: unable to read file {filename} '
                    '-- skipping. ({exceptionName}: {exception})').format(
                    filename=self.options.filename,
                    exceptionName=e.__class__.__name__,
                    exception=e
                )
        return (valid, errorMessage)

    def is_valid_zenpack(self):
        '''Determine if ZenPack is valid'''
        self.connect()
        self.app = self.dmd
        try:
            zenpack = self.dmd.ZenPackManager.packs._getOb(self.options.zenpack)
        except AttributeError:
            zenpack = None
        if zenpack is None:
            return False
        return True

    def parseOptions(self):
        """
        Uses the optparse parse previously populated and performs common options.
        """

        if self.noopts:
            args = []
        else:
            args = self.inputArgs

        (self.options, self.args) = self.parser.parse_args(args=args)
        self.options.filename = None
        self.options.zenpack = None
        self.options.device = None
        # check that necessary options are supplied
        # requires filename
        if len(self.args) != 1:
            self.parser.print_help()
            self.parser.exit(1)

        if self.options.lint or self.options.diagram or self.options.optimize:

            self.parser.usage = "%prog [options] FILENAME"
            if len(self.args) != 1:
                self.parser.error('No filename given')

            # set filename if given
            self.options.filename = self.args[0]
            # check validity of file
            is_valid, msg = self.is_valid_file()
            if not is_valid:
                self.parser.error(msg)

        if self.options.dump or self.options.create or\
           self.options.dump_event_classes or self.options.dump_process_classes:
            self.parser.usage = "%prog [options] ZENPACKNAME"
            if len(self.args) != 1:
                self.parser.error('No ZenPack given')

            self.options.zenpack = self.args[0]

        if self.options.paths:
            self.parser.usage = "%prog [options] DEVICE"
            if len(self.args) != 1:
                self.parser.error('No device given')
            self.options.device = self.args[0]

    def run(self):
        """run the specified function"""
        if self.options.dump:
            if not self.is_valid_zenpack():
                self.parser.error('{} was not found'.format(self.options.zenpack))

        if self.options.create:
            self.create_zenpack_srcdir(self.options.zenpack)

        elif self.options.dump:
            self.dump_templates(self.options.zenpack)

        elif self.options.lint:
            self.lint(self.options.filename)

        elif self.options.optimize:
            self.optimize(self.options.filename)

        elif self.options.diagram:
            self.class_diagram('yuml', self.options.filename)

        elif self.options.paths:
            self.list_paths()

        elif self.options.dump_event_classes:
            self.dump_event_classes(self.options.zenpack)

        elif self.options.dump_process_classes:
            self.dump_process_classes(self.options.zenpack)

    def optimize(self, filename):
        '''return formatted YAML with DEFAULTS optimized'''
        try:
            new_yaml = optimize_yaml(filename)
            print new_yaml
        except Exception, e:
            DEFAULTLOG.exception(e)

    @classmethod
    def lint(cls, filename):
        '''parse YAML file and check syntax'''
        handler = logging.StreamHandler(sys.stdout)

        DEFAULTLOG.addHandler(handler)

        try:
            load_yaml_single(filename, loader=WarningLoader)
        except Exception, e:
            DEFAULTLOG.exception(e)

    def validate_zenpack_name(self, zenpack_name):
        """Ensure that ZenPack name conforms with convention"""
        zenpack_name_parts = zenpack_name.split('.')

        if len(zenpack_name_parts) < 3:
            sys.exit('ZenPack name must conform to naming convention:\n\n    ZenPacks.<namespace.PackName>\n\n'\
                     'where "namespace" can contain multiple dot-delimited segments.  For example:\n\n   '\
                     ' ZenPacks.<namespace.subspace.PackName>\n\nis also a valid ZenPack name.')
        if zenpack_name_parts[0] != 'ZenPacks':
            suggested = "ZenPacks.{}".format('.'.join(zenpack_name_parts[1:]))
            sys.exit('ZenPack name must begin with "ZenPacks", got {}.  Suggest using "{}" instead.'.format(
                                                    zenpack_name_parts[0], suggested))

    def create_zenpack_srcdir(self, zenpack_name):
        """Create a new ZenPack source directory."""
        import errno

        self.validate_zenpack_name(zenpack_name)

        if os.path.exists(zenpack_name):
            sys.exit("{} directory already exists.".format(zenpack_name))

        print "Creating source directory for {}:".format(zenpack_name)

        zenpack_name_parts = zenpack_name.split('.')

        packages = reduce(
            lambda x, y: x + ['.'.join((x[-1], y))],
            zenpack_name_parts[1:],
            ['ZenPacks'])

        namespace_packages = packages[:-1]

        # Create ZenPacks.example.Thing/ZenPacks/example/Thing directory.
        module_directory = os.path.join(zenpack_name, *zenpack_name_parts)

        try:
            print "  - making directory: {}".format(module_directory)
            os.makedirs(module_directory)
        except OSError as e:
            if e.errno == errno.EEXIST:
                sys.exit("{} directory already exists.".format(zenpack_name))
            else:
                sys.exit(
                    "Failed to create {!r} directory: {}"
                    .format(zenpack_name, e.strerror))

        # Create setup.py.
        setup_py_fname = os.path.join(zenpack_name, 'setup.py')
        print "  - creating file: {}".format(setup_py_fname)
        with open(setup_py_fname, 'w') as setup_py_f:
            setup_py_f.write(
                SETUP_PY.format(
                    zenpack_name=zenpack_name,
                    namespace_packages=namespace_packages,
                    packages=packages))

        # Create MANIFEST.in.
        manifest_in_fname = os.path.join(zenpack_name, 'MANIFEST.in')
        print "  - creating file: {}".format(manifest_in_fname)
        with open(manifest_in_fname, 'w') as manifest_in_f:
            manifest_in_f.write("graft ZenPacks\n")

        # Create __init__.py files in all namespace directories.
        for namespace_package in namespace_packages:
            namespace_init_fname = os.path.join(
                zenpack_name,
                os.path.join(*namespace_package.split('.')),
                '__init__.py')

            print "  - creating file: {}".format(namespace_init_fname)
            with open(namespace_init_fname, 'w') as namespace_init_f:
                namespace_init_f.write(
                    "__import__('pkg_resources').declare_namespace(__name__)\n")

        # Create common subdirectories
        subdirs = ['datasources', 'thresholds', 'parsers',
                   'migrate', 'resources', 'modeler', 'tests',
                   'libexec', 'modeler/plugins', 'lib']
        for subdir in subdirs:
            dirpath = os.path.join(module_directory, subdir)
            os.makedirs(dirpath)
            init_fname = os.path.join(dirpath, '__init__.py')
            print "  - creating file: {}".format(init_fname)
            with open(init_fname, 'w') as init_f:
                init_f.write("\n")

        # Create __init__.py in ZenPack module directory.
        init_fname = os.path.join(module_directory, '__init__.py')
        print "  - creating file: {}".format(init_fname)
        with open(init_fname, 'w') as init_f:
            init_f.write(
                "import os\n"
                "from ZenPacks.zenoss.ZenPackLib import zenpacklib\n\n"
                "CFG = zenpacklib.load_yaml("
                "[os.path.join(os.path.dirname(__file__), \"zenpack.yaml\")]"
                ", verbose=False, level=30)\n"
                "schema = CFG.zenpack_module.schema\n")

        # Create zenpack.yaml in ZenPack module directory.
        yaml_fname = os.path.join(module_directory, 'zenpack.yaml')
        print "  - creating file: {}".format(yaml_fname)
        with open(yaml_fname, 'w') as yaml_f:
            yaml_f.write("name: {}\n".format(zenpack_name))

    def dump_templates(self, zenpack_name):
        ''''''
        self.connect()

        templates = self.zenpack_templatespecs(zenpack_name)
        if templates:
            zpsp = ZenPackSpecParams(
                zenpack_name,
                device_classes={x: {} for x in templates})

            for dc_name in templates:
                zpsp.device_classes[dc_name].templates = templates[dc_name]

            print yaml.dump(zpsp, Dumper=Dumper)

    def class_diagram(self, diagram_type, filename):
        ''''''
        with open(filename, 'r') as stream:
            CFG = yaml.load(stream, Loader=ZenPackSpecLoader)

        if diagram_type == 'yuml':
            print "# Classes"
            for cname in sorted(CFG.classes):
                print "[{}]".format(cname)

            print "\n# Inheritence"
            for cname in CFG.classes:
                cspec = CFG.classes[cname]
                for baseclass in cspec.bases:
                    if type(baseclass) != str:
                        baseclass = aq_base(baseclass).__name__
                    print "[{}]^-[{}]".format(baseclass, cspec.name)

            print "\n# Containing Relationships"
            for crspec in CFG.class_relationships:
                if crspec.cardinality == '1:MC':
                    print "[{}]++{}-{}[{}]".format(
                        crspec.left_class, crspec.left_relname,
                        crspec.right_relname, crspec.right_class)

            print "\n# Non-Containing Relationships"
            for crspec in CFG.class_relationships:
                if crspec.cardinality == '1:1':
                    print "[{}]{}-.-{}[{}]".format(
                        crspec.left_class, crspec.left_relname,
                        crspec.right_relname, crspec.right_class)
                if crspec.cardinality == '1:M':
                    print "[{}]{}-.-{}++[{}]".format(
                        crspec.left_class, crspec.left_relname,
                        crspec.right_relname, crspec.right_class)
                if crspec.cardinality == 'M:M':
                    print "[{}]++{}-.-{}++[{}]".format(
                        crspec.left_class, crspec.left_relname,
                        crspec.right_relname, crspec.right_class)
        else:
            DEFAULTLOG.error("Diagram type '{}' is not supported.".format(diagram_type))

    def list_paths(self):
        ''''''
        self.connect()
        device = self.dmd.Devices.findDevice(self.options.device)
        if device is None:
            DEFAULTLOG.error("Device '{}' not found.".format(self.options.device))
            return

        from Acquisition import aq_chain
        from Products.ZenRelations.RelationshipBase import RelationshipBase

        all_paths = set()
        included_paths = set()
        class_summary = collections.defaultdict(set)

        for component in device.getDeviceComponents():
            for facet in component.get_facets(recurse_all=True):
                path = []
                for obj in aq_chain(facet):
                    if obj == component:
                        break
                    if isinstance(obj, RelationshipBase):
                        path.insert(0, obj.id)
                all_paths.add(component.meta_type + ":" + "/".join(path) + ":" + facet.meta_type)

            for facet in component.get_facets():
                path = []
                for obj in aq_chain(facet):
                    if obj == component:
                        break
                    if isinstance(obj, RelationshipBase):
                        path.insert(0, obj.id)
                all_paths.add(component.meta_type + ":" + "/".join(path) + ":" + facet.meta_type)
                included_paths.add(component.meta_type + ":" + "/".join(path) + ":" + facet.meta_type)
                class_summary[component.meta_type].add(facet.meta_type)

        print "Paths\n-----\n"
        for path in sorted(all_paths):
            if path in included_paths:
                if "/" not in path:
                    # normally all direct relationships are included
                    print "DIRECT  " + path
                else:
                    # sometimes extra paths are pulled in due to extra_paths
                    # configuration.
                    print "EXTRA   " + path
            else:
                print "EXCLUDE " + path

        print "\nClass Summary\n-------------\n"
        for source_class in sorted(class_summary.keys()):
            print "{} is reachable from {}".format(source_class, ", ".join(sorted(class_summary[source_class])))

    def zenpack_templatespecs(self, zenpack_name):
        """Return dictionary of RRDTemplateSpecParams by device_class.

        Example return value:

            {
                '/Server/Linux': {
                    'Device': RRDTemplateSpecParams(...),
                },
                '/Server/SSH/Linux': {
                    'Device': RRDTemplateSpecParams(...),
                    'IpInterface': RRDTemplateSpecParams(...),
                },
            }

        """
        self.connect()
        zenpack = self.dmd.ZenPackManager.packs._getOb(zenpack_name, None)
        if zenpack is None:
            DEFAULTLOG.error("ZenPack '{}' not found.".format(zenpack_name))
            return

        # Find explicitly associated templates, and templates implicitly
        # associated through an explicitly associated device class.
        from Products.ZenModel.DeviceClass import DeviceClass
        from Products.ZenModel.RRDTemplate import RRDTemplate

        templates = []
        for packable in zenpack.packables():
            if isinstance(packable, DeviceClass):
                templates.extend(packable.getAllRRDTemplates())
            elif isinstance(packable, RRDTemplate):
                templates.append(packable)

        # Only create specs for templates that have an associated device
        # class. This prevents locally-overridden templates from being
        # included.
        specs = collections.defaultdict(dict)
        for template in templates:
            deviceClass = template.deviceClass()
            if deviceClass:
                dc_name = deviceClass.getOrganizerName()
                spec = RRDTemplateSpecParams.fromObject(template)
                specs[dc_name][template.id] = spec

        return specs

    def dump_event_classes(self, zenpack_name):
        self.connect()
        eventclasses = self.zenpack_eventclassspecs(zenpack_name)
        if eventclasses:
            zpsp = ZenPackSpecParams(zenpack_name,
                                     event_classes={x: {} for x in eventclasses})
            for ec_name in eventclasses:
                zpsp.event_classes[ec_name] = eventclasses[ec_name]
                zpsp.event_classes[ec_name].mappings = eventclasses[ec_name].mappings

            print yaml.dump(zpsp, Dumper=Dumper)

    def zenpack_eventclassspecs(self, zenpack_name):
        zenpack = self.dmd.ZenPackManager.packs._getOb(zenpack_name, None)
        if zenpack is None:
            self.LOG.error("ZenPack '%s' not found." % zenpack_name)
            return

        eventclasses = collections.defaultdict(dict)
        for eventclass in [x for x in zenpack.packables() if x.meta_type == 'EventClass']:
            ec_name = eventclass.getDmdKey()
            eventclasses[ec_name] = EventClassSpecParams.fromObject(eventclass, remove=True)
            for subclass in eventclass.getSubEventClasses():
                # Remove = false because the removing the parent will remove the child # This is a performance optimization
                eventclasses[ec_name] = EventClassSpecParams.fromObject(subclass, remove=False)

        # get list of instances associated with event classes not already seen
        instances = [x for x in zenpack.packables() if x.meta_type == 'EventClassInst' and x.eventClass().getDmdKey() not in eventclasses]
        # list of unique event classes
        inst_evs = list({x.eventClass() for x in instances})

        for ev in inst_evs:
            ec_name = ev.getDmdKey()
            ev_spec = EventClassSpecParams.new(ec_name, remove=False)
            ev_instances = [x for x in ev.instances() if x in zenpack.packables()]
            ev_spec.mappings = { x.id: EventClassMappingSpecParams.fromObject(x, remove=True) for x in ev_instances }
            eventclasses[ec_name] = ev_spec

        return eventclasses

    def dump_process_classes(self, zenpack_name):
        self.connect()
        processclasses = self.zenpack_processclassspecs(zenpack_name)
        if processclasses:
            zpsp = ZenPackSpecParams(zenpack_name,
                                     process_class_organizers={x: {} for x in processclasses})
            for pc_name in processclasses:
                zpsp.process_class_organizers[pc_name].process_classes = processclasses[pc_name].process_classes

            print yaml.dump(zpsp, Dumper=Dumper)

    def zenpack_processclassspecs(self, zenpack_name):
        zenpack = self.dmd.ZenPackManager.packs._getOb(zenpack_name, None)
        if zenpack is None:
            self.LOG.error("ZenPack '%s' not found.", zenpack_name)
            return

        processclasses = collections.defaultdict(dict)
        for processclassorg in [x for x in zenpack.packables() if x.meta_type == 'OSProcessOrganizer']:
            pc_name = processclassorg.getDmdKey()
            processclasses[pc_name] = ProcessClassOrganizerSpecParams.fromObject(processclassorg)
            for subclass in processclassorg.getSubOrganizers():
                pc_name = subclass.getDmdKey()
                processclasses[pc_name] = ProcessClassOrganizerSpecParams.fromObject(subclass)

        return processclasses
