#
# Utility functions for the command line drivers
#
# Copyright 2006-2007, 2013, 2014 Red Hat, Inc.
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import argparse
import collections
import logging
import logging.handlers
import os
import re
import shlex
import subprocess
import sys
import traceback
import types

import libvirt

from virtcli import CLIConfig

from . import util
from .devices import (Device, DeviceController, DeviceDisk, DeviceGraphics,
        DeviceInterface, DevicePanic)
from .domain import DomainClock, DomainOs
from .nodedev import NodeDevice
from .storage import StoragePool, StorageVolume
from .unattended import UnattendedData


##########################
# Global option handling #
##########################

class _GlobalState(object):
    def __init__(self):
        self.quiet = False

        self.all_checks = None
        self._validation_checks = {}

    def set_validation_check(self, checkname, val):
        self._validation_checks[checkname] = val

    def get_validation_check(self, checkname):
        if self.all_checks is not None:
            return self.all_checks

        # Default to True for all checks
        return self._validation_checks.get(checkname, True)


_globalstate = None


def get_global_state():
    return _globalstate


def _reset_global_state():
    global _globalstate
    _globalstate = _GlobalState()


VIRT_PARSERS = []


####################
# CLI init helpers #
####################

class VirtHelpFormatter(argparse.RawDescriptionHelpFormatter):
    '''
    Subclass the default help formatter to allow printing newline characters
    in --help output. The way we do this is a huge hack :(

    Inspiration: http://groups.google.com/group/comp.lang.python/browse_thread/thread/6df6e6b541a15bc2/09f28e26af0699b1
    '''
    oldwrap = None

    # pylint: disable=arguments-differ
    def _split_lines(self, *args, **kwargs):
        def return_default():
            return argparse.RawDescriptionHelpFormatter._split_lines(
                self, *args, **kwargs)

        if len(kwargs) != 0 and len(args) != 2:
            return return_default()

        try:
            text = args[0]
            if "\n" in text:
                return text.splitlines()
            return return_default()
        except Exception:
            return return_default()


def setupParser(usage, description, introspection_epilog=False):
    epilog = _("See man page for examples and full option syntax.")
    if introspection_epilog:
        epilog = _("Use '--option=?' or '--option help' to see "
            "available suboptions") + "\n" + epilog

    parser = argparse.ArgumentParser(
        usage=usage, description=description,
        formatter_class=VirtHelpFormatter,
        epilog=epilog)
    parser.add_argument('--version', action='version',
                        version=CLIConfig.version)

    return parser


def earlyLogging():
    logging.basicConfig(level=logging.DEBUG, format='%(message)s')


def setupLogging(appname, debug_stdout, do_quiet, cli_app=True):
    _reset_global_state()
    get_global_state().quiet = do_quiet

    vi_dir = None
    logfile = None
    if not in_testsuite():
        vi_dir = util.get_cache_dir()
        logfile = os.path.join(vi_dir, appname + ".log")

    try:
        if vi_dir and not os.access(vi_dir, os.W_OK):
            if os.path.exists(vi_dir):
                raise RuntimeError("No write access to directory %s" % vi_dir)

            try:
                os.makedirs(vi_dir, 0o751)
            except IOError as e:
                raise RuntimeError("Could not create directory %s: %s" %
                                   (vi_dir, e))

        if (logfile and
            os.path.exists(logfile) and
            not os.access(logfile, os.W_OK)):
            raise RuntimeError("No write access to logfile %s" % logfile)
    except Exception as e:
        logging.warning("Error setting up logfile: %s", e)
        logfile = None


    dateFormat = "%a, %d %b %Y %H:%M:%S"
    fileFormat = ("[%(asctime)s " + appname + " %(process)d] "
                  "%(levelname)s (%(module)s:%(lineno)d) %(message)s")
    streamErrorFormat = "%(levelname)-8s %(message)s"

    rootLogger = logging.getLogger()

    # Undo early logging
    for handler in rootLogger.handlers:
        rootLogger.removeHandler(handler)

    rootLogger.setLevel(logging.DEBUG)
    if logfile:
        fileHandler = logging.handlers.RotatingFileHandler(
            logfile, "ae", 1024 * 1024, 5)
        fileHandler.setFormatter(
            logging.Formatter(fileFormat, dateFormat))
        rootLogger.addHandler(fileHandler)

    streamHandler = logging.StreamHandler(sys.stderr)
    if debug_stdout:
        streamHandler.setLevel(logging.DEBUG)
        streamHandler.setFormatter(logging.Formatter(fileFormat,
                                                     dateFormat))
    elif cli_app or not logfile:
        if get_global_state().quiet:
            level = logging.ERROR
        else:
            level = logging.WARN
        streamHandler.setLevel(level)
        streamHandler.setFormatter(logging.Formatter(streamErrorFormat))
    else:
        streamHandler = None

    if streamHandler:
        rootLogger.addHandler(streamHandler)

    util.register_libvirt_error_handler()

    # Log uncaught exceptions
    def exception_log(typ, val, tb):
        logging.debug("Uncaught exception:\n%s",
                      "".join(traceback.format_exception(typ, val, tb)))
        if not debug_stdout:
            # If we are already logging to stdout, don't double print
            # the backtrace
            sys.__excepthook__(typ, val, tb)
    sys.excepthook = exception_log

    logging.getLogger("requests").setLevel(logging.ERROR)

    # Log the app command string
    logging.debug("Launched with command line: %s", " ".join(sys.argv))


def in_testsuite():
    return "VIRTINST_TEST_SUITE" in os.environ


##############################
# Libvirt connection helpers #
##############################

def getConnection(uri):
    from .connection import VirtinstConnection

    logging.debug("Requesting libvirt URI %s", (uri or "default"))
    conn = VirtinstConnection(uri)
    conn.open(_openauth_cb, None)
    logging.debug("Received libvirt URI %s", conn.uri)

    return conn


def _openauth_cb(creds, _cbdata):
    for cred in creds:
        # Libvirt virConnectCredential
        credtype, prompt, _challenge, _defresult, _result = cred
        noecho = credtype in [
                libvirt.VIR_CRED_PASSPHRASE, libvirt.VIR_CRED_NOECHOPROMPT]
        if not prompt:
            logging.error("No prompt for auth credtype=%s", credtype)
            return -1
        logging.debug("openauth_cb prompt=%s", prompt)

        prompt += ": "
        if noecho:
            import getpass
            res = getpass.getpass(prompt)
        else:
            res = input(prompt)

        # Overwriting 'result' is how we return values to libvirt
        cred[-1] = res
    return 0


##############################
# Misc CLI utility functions #
##############################

def fail(msg, do_exit=True):
    """
    Convenience function when failing in cli app
    """
    logging.debug("".join(traceback.format_stack()))
    logging.error(msg)
    if sys.exc_info()[0] is not None:
        logging.debug("", exc_info=True)
    if do_exit:
        _fail_exit()


def print_stdout(msg, do_force=False):
    if do_force or not get_global_state().quiet:
        print(msg)


def print_stderr(msg):
    logging.debug(msg)
    print(msg, file=sys.stderr)


def _fail_exit():
    sys.exit(1)


def nice_exit():
    print_stdout(_("Exiting at user request."))
    sys.exit(0)


def virsh_start_cmd(guest):
    return ("virsh --connect %s start %s" % (guest.conn.uri, guest.name))


def install_fail(guest):
    virshcmd = virsh_start_cmd(guest)

    print_stderr(
        _("Domain installation does not appear to have been successful.\n"
          "If it was, you can restart your domain by running:\n"
          "  %s\n"
          "otherwise, please restart your installation.") % virshcmd)
    sys.exit(1)


def set_prompt(prompt):
    # Set whether we allow prompts, or fail if a prompt pops up
    if prompt:
        logging.warning("--prompt mode is no longer supported.")


def validate_disk(dev, warn_overwrite=False):
    def _optional_fail(msg, checkname, warn_on_skip=True):
        do_check = get_global_state().get_validation_check(checkname)
        if do_check:
            fail(msg + (_(" (Use --check %s=off or "
                "--check all=off to override)") % checkname))

        logging.debug("Skipping --check %s error condition '%s'",
            checkname, msg)
        if warn_on_skip:
            logging.warning(msg)

    def check_path_exists(dev):
        """
        Prompt if disk file already exists and preserve mode is not used
        """
        if not warn_overwrite:
            return
        if not DeviceDisk.path_definitely_exists(dev.conn, dev.path):
            return
        _optional_fail(
            _("This will overwrite the existing path '%s'") % dev.path,
            "path_exists")

    def check_inuse_conflict(dev):
        """
        Check if disk is inuse by another guest
        """
        names = dev.is_conflict_disk()
        if not names:
            return

        _optional_fail(_("Disk %s is already in use by other guests %s." %
            (dev.path, names)),
            "path_in_use")

    def check_size_conflict(dev):
        """
        Check if specified size exceeds available storage
        """
        isfatal, errmsg = dev.is_size_conflict()
        # The isfatal case should have already caused us to fail
        if not isfatal and errmsg:
            _optional_fail(errmsg, "disk_size", warn_on_skip=False)

    def check_path_search(dev):
        searchdata = dev.check_path_search(dev.conn, dev.path)
        if not searchdata.fixlist:
            return
        logging.warning(_("%s may not be accessible by the hypervisor. "
            "You will need to grant the '%s' user search permissions for "
            "the following directories: %s"),
            dev.path, searchdata.user, searchdata.fixlist)

    check_path_exists(dev)
    check_inuse_conflict(dev)
    check_size_conflict(dev)
    check_path_search(dev)


def _run_console(domain, args):
    ignore = domain
    logging.debug("Running: %s", " ".join(args))
    if in_testsuite():
        print_stdout("testsuite console command: %s" % args)
        args = ["/bin/true"]

    child = os.fork()
    if child:
        return child

    os.execvp(args[0], args)
    os._exit(1)  # pylint: disable=protected-access


def _gfx_console(guest, domain):
    args = ["virt-viewer",
            "--connect", guest.conn.uri,
            "--wait", guest.name]

    # Currently virt-viewer needs attaching to the local display while
    # spice gl is enabled or listen type none is used.
    if guest.has_gl() or guest.has_listen_none():
        args.append("--attach")

    logging.debug("Launching virt-viewer for graphics type '%s'",
        guest.devices.graphics[0].type)
    return _run_console(domain, args)


def _txt_console(guest, domain):
    args = ["virsh",
            "--connect", guest.conn.uri,
            "console", guest.name]

    logging.debug("Connecting to text console")
    return _run_console(domain, args)


def connect_console(guest, domain, consolecb, wait, destroy_on_exit):
    """
    Launched the passed console callback for the already defined
    domain. If domain isn't running, return an error.
    """
    child = None
    if consolecb:
        child = consolecb(guest, domain)

    if not child or not wait:
        return

    # If we connected the console, wait for it to finish
    try:
        os.waitpid(child, 0)
    except OSError as e:
        logging.debug("waitpid error: %s", e)

    if destroy_on_exit and domain.isActive():
        logging.debug("console exited and destroy_on_exit passed, destroying")
        domain.destroy()


def get_console_cb(guest):
    gdevs = guest.devices.graphics
    if not gdevs:
        return _txt_console

    gtype = gdevs[0].type
    if gtype not in ["default",
            DeviceGraphics.TYPE_VNC,
            DeviceGraphics.TYPE_SPICE]:
        logging.debug("No viewer to launch for graphics type '%s'", gtype)
        return

    if not in_testsuite():
        try:
            subprocess.check_output(["virt-viewer", "--version"])
        except OSError:
            logging.warning(_("Unable to connect to graphical console: "
                           "virt-viewer not installed. Please install "
                           "the 'virt-viewer' package."))
            return None

        if not os.environ.get("DISPLAY", ""):
            logging.warning(_("Graphics requested but DISPLAY is not set. "
                           "Not running virt-viewer."))
            return None

    return _gfx_console


def get_meter():
    quiet = (get_global_state().quiet or in_testsuite())
    return util.make_meter(quiet=quiet)


###########################
# bash completion helpers #
###########################

def _get_completer_parsers():
    return VIRT_PARSERS + [ParserCheck, ParserLocation, ParserOSVariant,
            ParserUnattended]


def _virtparser_completer(prefix, **kwargs):
    sub_options = []
    for parserclass in _get_completer_parsers():
        if kwargs['action'].dest == parserclass.cli_arg_name:
            # pylint: disable=protected-access
            for arg in sorted(parserclass._virtargs, key=lambda p: p.cliname):
                sub_options.append(arg.cliname + "=")
    entered_options = prefix.split(",")
    for option in entered_options:
        pos = option.find("=")
        if pos > 0 and option[: pos + 1] in sub_options:
            sub_options.remove(option[: pos + 1])
    return sub_options


def _completer_validator(current_input, keyword_to_check_against):
    entered_options = keyword_to_check_against.split(",")

    # e.g. for: --disk <TAB><TAB>
    if keyword_to_check_against == "":
        return True
    # e.g. for: --disk bu<TAB><TAB> or --disk bus=ide,<TAB><TAB>
    #                               or --disk bus=ide,pa<TAB><TAB>
    if (len(entered_options) >= 1 and "=" not in entered_options[-1]):
        if entered_options[-1] == "":
            return True
        else:
            return current_input.startswith(entered_options[-1])


def autocomplete(parser):
    if "_ARGCOMPLETE" not in os.environ:
        return

    import argcomplete

    parsernames = [pclass.cli_flag_name() for pclass in
                   _get_completer_parsers()]
    # pylint: disable=protected-access
    for action in parser._actions:
        for opt in action.option_strings:
            if opt in parsernames:
                action.completer = _virtparser_completer
                break

    kwargs = {"validator": _completer_validator}
    if in_testsuite():
        import io
        kwargs["output_stream"] = io.BytesIO()
        kwargs["exit_method"] = sys.exit

    try:
        argcomplete.autocomplete(parser, **kwargs)
    except SystemExit:
        if in_testsuite():
            output = kwargs["output_stream"].getvalue().decode("utf-8")
            print(output)
        raise


###########################
# Common CLI option/group #
###########################

def add_connect_option(parser, invoker=None):
    if invoker == "virt-xml":
        parser.add_argument("-c", "--connect", metavar="URI",
                help=_("Connect to hypervisor with libvirt URI"))
    else:
        parser.add_argument("--connect", metavar="URI",
                help=_("Connect to hypervisor with libvirt URI"))


def add_misc_options(grp, prompt=False, replace=False,
                     printxml=False, printstep=False,
                     noreboot=False, dryrun=False,
                     noautoconsole=False):
    if prompt:
        grp.add_argument("--prompt", action="store_true",
                        default=False, help=argparse.SUPPRESS)
        grp.add_argument("--force", action="store_true",
                        default=False, help=argparse.SUPPRESS)

    if noautoconsole:
        grp.add_argument("--noautoconsole", action="store_false",
            dest="autoconsole", default=True,
            help=_("Don't automatically try to connect to the guest console"))

    if noreboot:
        grp.add_argument("--noreboot", action="store_true",
                       help=_("Don't boot guest after completing install."))

    if replace:
        grp.add_argument("--replace", action="store_true",
            help=_("Don't check name collision, overwrite any guest "
                   "with the same name."))

    if printxml:
        print_kwargs = {
            "dest": "xmlonly",
            "default": False,
            "help": _("Print the generated domain XML rather than create "
                "the guest."),
        }

        if printstep:
            print_kwargs["nargs"] = "?"
            print_kwargs["const"] = "all"
        else:
            print_kwargs["action"] = "store_true"

        grp.add_argument("--print-xml", **print_kwargs)
        if printstep:
            # Back compat, argparse allows us to use --print-xml
            # for everything.
            grp.add_argument("--print-step", dest="xmlstep",
                help=argparse.SUPPRESS)

    if dryrun:
        grp.add_argument("--dry-run", action="store_true", dest="dry",
                       help=_("Run through install process, but do not "
                              "create devices or define the guest."))

    if prompt:
        grp.add_argument("--check", action="append",
            help=_("Enable or disable validation checks. Example:\n"
                   "--check path_in_use=off\n"
                   "--check all=off"))
    grp.add_argument("-q", "--quiet", action="store_true",
                   help=_("Suppress non-error output"))
    grp.add_argument("-d", "--debug", action="store_true",
                   help=_("Print debugging information"))


def add_metadata_option(grp):
    ParserMetadata.register()
    grp.add_argument("--metadata", action="append",
        help=_("Configure guest metadata. Ex:\n"
        "--metadata name=foo,title=\"My pretty title\",uuid=...\n"
        "--metadata description=\"My nice long description\""))


def add_memory_option(grp, backcompat=False):
    ParserMemory.register()
    grp.add_argument("--memory", action="append",
        help=_("Configure guest memory allocation. Ex:\n"
               "--memory 1024 (in MiB)\n"
               "--memory 512,maxmemory=1024\n"
               "--memory 512,maxmemory=1024,hotplugmemorymax=2048,"
               "hotplugmemoryslots=2"))
    if backcompat:
        grp.add_argument("-r", "--ram", type=int, dest="oldmemory",
            help=argparse.SUPPRESS)


def vcpu_cli_options(grp, backcompat=True, editexample=False):
    # The order of the parser registration is important here!
    ParserCPU.register()
    ParserVCPU.register()
    grp.add_argument("--vcpus", action="append",
        help=_("Number of vcpus to configure for your guest. Ex:\n"
               "--vcpus 5\n"
               "--vcpus 5,maxvcpus=10,cpuset=1-4,6,8\n"
               "--vcpus sockets=2,cores=4,threads=2"))

    extramsg = "--cpu host"
    if editexample:
        extramsg = "--cpu host-model,clearxml=yes"
    grp.add_argument("--cpu", action="append",
        help=_("CPU model and features. Ex:\n"
               "--cpu coreduo,+x2apic\n"
               "--cpu host-passthrough\n") + extramsg)

    if backcompat:
        grp.add_argument("--check-cpu", action="store_true",
                         help=argparse.SUPPRESS)
        grp.add_argument("--cpuset", help=argparse.SUPPRESS)


def add_gfx_option(devg):
    ParserGraphics.register()
    devg.add_argument("--graphics", action="append",
      help=_("Configure guest display settings. Ex:\n"
             "--graphics vnc\n"
             "--graphics spice,port=5901,tlsport=5902\n"
             "--graphics none\n"
             "--graphics vnc,password=foobar,port=5910,keymap=ja"))


def add_net_option(devg):
    ParserNetwork.register()
    devg.add_argument("-w", "--network", action="append",
      help=_("Configure a guest network interface. Ex:\n"
             "--network bridge=mybr0\n"
             "--network network=my_libvirt_virtual_net\n"
             "--network network=mynet,model=virtio,mac=00:11...\n"
             "--network none\n"
             "--network help"))


def add_device_options(devg, sound_back_compat=False):
    ParserController.register()
    devg.add_argument("--controller", action="append",
        help=_("Configure a guest controller device. Ex:\n"
               "--controller type=usb,model=qemu-xhci\n"
               "--controller virtio-scsi\n"))
    ParserInput.register()
    devg.add_argument("--input", action="append",
        help=_("Configure a guest input device. Ex:\n"
               "--input tablet\n"
               "--input keyboard,bus=usb"))
    ParserSerial.register()
    devg.add_argument("--serial", action="append",
                    help=_("Configure a guest serial device"))
    ParserParallel.register()
    devg.add_argument("--parallel", action="append",
                    help=_("Configure a guest parallel device"))
    ParserChannel.register()
    devg.add_argument("--channel", action="append",
                    help=_("Configure a guest communication channel"))
    ParserConsole.register()
    devg.add_argument("--console", action="append",
                    help=_("Configure a text console connection between "
                           "the guest and host"))
    ParserHostdev.register()
    devg.add_argument("--hostdev", action="append",
                    help=_("Configure physical USB/PCI/etc host devices "
                           "to be shared with the guest"))
    # Back compat name
    devg.add_argument("--host-device", action="append", dest="hostdev",
                    help=argparse.SUPPRESS)

    ParserFilesystem.register()
    devg.add_argument("--filesystem", action="append",
        help=_("Pass host directory to the guest. Ex: \n"
               "--filesystem /my/source/dir,/dir/in/guest\n"
               "--filesystem template_name,/,type=template"))

    ParserSound.register()
    # --sound used to be a boolean option, hence the nargs handling
    sound_kwargs = {
        "action": "append",
        "help": _("Configure guest sound device emulation"),
    }
    if sound_back_compat:
        sound_kwargs["nargs"] = '?'
    devg.add_argument("--sound", **sound_kwargs)
    if sound_back_compat:
        devg.add_argument("--soundhw", action="append", dest="sound",
            help=argparse.SUPPRESS)

    ParserWatchdog.register()
    devg.add_argument("--watchdog", action="append",
                    help=_("Configure a guest watchdog device"))
    ParserVideo.register()
    devg.add_argument("--video", action="append",
                    help=_("Configure guest video hardware."))
    ParserSmartcard.register()
    devg.add_argument("--smartcard", action="append",
                    help=_("Configure a guest smartcard device. Ex:\n"
                           "--smartcard mode=passthrough"))
    ParserRedir.register()
    devg.add_argument("--redirdev", action="append",
                    help=_("Configure a guest redirection device. Ex:\n"
                           "--redirdev usb,type=tcp,server=192.168.1.1:4000"))
    ParserMemballoon.register()
    devg.add_argument("--memballoon", action="append",
                    help=_("Configure a guest memballoon device. Ex:\n"
                           "--memballoon model=virtio"))
    ParserTPM.register()
    devg.add_argument("--tpm", action="append",
                    help=_("Configure a guest TPM device. Ex:\n"
                           "--tpm /dev/tpm"))
    ParserRNG.register()
    devg.add_argument("--rng", action="append",
                    help=_("Configure a guest RNG device. Ex:\n"
                           "--rng /dev/urandom"))
    ParserPanic.register()
    devg.add_argument("--panic", action="append",
                    help=_("Configure a guest panic device. Ex:\n"
                           "--panic default"))
    ParserMemdev.register()
    devg.add_argument("--memdev", action="append",
                    help=_("Configure a guest memory device. Ex:\n"
                           "--memdev dimm,target.size=1024"))
    ParserVsock.register()
    devg.add_argument("--vsock", action="append",
                    help=_("Configure guest vsock sockets. Ex:\n"
                           "--vsock auto_cid=yes\n"
                           "--vsock cid=7"))


def add_guest_xml_options(geng):
    ParserSecurity.register()
    geng.add_argument("--security", action="append",
        help=_("Set domain security driver configuration."))

    ParserCputune.register()
    geng.add_argument("--cputune", action="append",
        help=_("Tune CPU parameters for the domain process."))

    ParserNumatune.register()
    geng.add_argument("--numatune", action="append",
        help=_("Tune NUMA policy for the domain process."))

    ParserMemtune.register()
    geng.add_argument("--memtune", action="append",
        help=_("Tune memory policy for the domain process."))

    ParserBlkiotune.register()
    geng.add_argument("--blkiotune", action="append",
        help=_("Tune blkio policy for the domain process."))

    ParserMemoryBacking.register()
    geng.add_argument("--memorybacking", action="append",
        help=_("Set memory backing policy for the domain process. Ex:\n"
               "--memorybacking hugepages=on"))

    ParserFeatures.register()
    geng.add_argument("--features", action="append",
        help=_("Set domain <features> XML. Ex:\n"
               "--features acpi=off\n"
               "--features apic=on,eoi=on"))

    ParserClock.register()
    geng.add_argument("--clock", action="append",
        help=_("Set domain <clock> XML. Ex:\n"
               "--clock offset=localtime,rtc_tickpolicy=catchup"))

    ParserPM.register()
    geng.add_argument("--pm", action="append",
        help=_("Configure VM power management features"))

    ParserEvents.register()
    geng.add_argument("--events", action="append",
        help=_("Configure VM lifecycle management policy"))

    ParserResource.register()
    geng.add_argument("--resource", action="append",
        help=_("Configure VM resource partitioning (cgroups)"))

    ParserSysinfo.register()
    geng.add_argument("--sysinfo", action="append",
        help=_("Configure SMBIOS System Information. Ex:\n"
               "--sysinfo host\n"
               "--sysinfo bios_vendor=MyVendor,bios_version=1.2.3,...\n"))

    ParserQemuCLI.register()
    geng.add_argument("--qemu-commandline", action="append",
        help=_("Pass arguments directly to the qemu emulator. Ex:\n"
               "--qemu-commandline='-display gtk,gl=on'\n"
               "--qemu-commandline env=DISPLAY=:0.1"))


def add_boot_options(insg):
    ParserBoot.register()
    insg.add_argument("--boot", action="append",
        help=_("Configure guest boot settings. Ex:\n"
               "--boot hd,cdrom,menu=on\n"
               "--boot init=/sbin/init (for containers)"))

    ParserIdmap.register()
    insg.add_argument("--idmap", action="append",
        help=_("Enable user namespace for LXC container. Ex:\n"
               "--idmap uid_start=0,uid_target=1000,uid_count=10"))


def add_disk_option(stog, editexample=False):
    ParserDisk.register()
    editmsg = ""
    if editexample:
        editmsg += "\n--disk cache=  (unset cache)"
    stog.add_argument("--disk", action="append",
        help=_("Specify storage with various options. Ex.\n"
               "--disk size=10 (new 10GiB image in default location)\n"
               "--disk /my/existing/disk,cache=none\n"
               "--disk device=cdrom,bus=scsi\n"
               "--disk=?") + editmsg)


def add_os_variant_option(parser, virtinstall):
    osg = parser.add_argument_group(_("OS options"))

    if virtinstall:
        msg = _("The OS being installed in the guest.")
    else:
        msg = _("The OS installed in the guest.")
    msg += "\n"
    msg += _("This is used for deciding optimal defaults like virtio.\n"
             "Example values: fedora29, rhel7.0, win10, ...\n"
             "See 'osinfo-query os' for a full list.")

    osg.add_argument("--os-variant", help=msg)
    return osg


#############################################
# CLI complex parsing helpers               #
# (for options like --disk, --network, etc. #
#############################################

def _raw_on_off_convert(s):
    tvalues = ["y", "yes", "1", "true", "t", "on"]
    fvalues = ["n", "no", "0", "false", "f", "off"]

    s = (s or "").lower()
    if s in tvalues:
        return True
    elif s in fvalues:
        return False
    return None


def _on_off_convert(key, val):
    if val is None:
        return None

    val = _raw_on_off_convert(val)
    if val is not None:
        return val
    raise fail(_("%(key)s must be 'yes' or 'no'") % {"key": key})


class _VirtCLIArgumentStatic(object):
    """
    Helper class to hold all of the static data we need for knowing
    how to parse a cli subargument, like --disk path=, or --network mac=.

    @cliname: The command line option name, 'path' for path=FOO
    @propname: The virtinst API attribute name the cliargument maps to.
    @cb: Rather than set a virtinst object property directly, use
        this callback instead. It should have the signature:
        cb(parser, inst, val, virtarg)

    @ignore_default: If the value passed on the cli is 'default', don't
        do anything.
    @can_comma: If True, this option is expected to have embedded commas.
        After the parser sees this option, it will iterate over the
        option string until it finds another known argument name:
        everything prior to that argument name is considered part of
        the value of this option, '=' included. Should be used sparingly.
    @aliases: List of cli aliases. Useful if we want to change a property
        name on the cli but maintain back compat.
    @is_onoff: The value expected on the cli is on/off or yes/no, convert
        it to true/false.
    @lookup_cb: If specified, use this function for performing match
        lookups.
    @find_inst_cb: If specified, this can be used to return a different
        'inst' to check and set attributes against. For example,
        DeviceDisk has multiple seclabel children, this provides a hook
        to lookup the specified child object.
    """
    def __init__(self, cliname, propname,
                 cb=None, can_comma=None,
                 ignore_default=False, aliases=None, is_onoff=False,
                 lookup_cb=-1, find_inst_cb=None):
        self.cliname = cliname
        self.propname = propname
        self.cb = cb
        self.can_comma = can_comma
        self.ignore_default = ignore_default
        self.aliases = aliases
        self.is_onoff = is_onoff
        self.lookup_cb = lookup_cb
        self.find_inst_cb = find_inst_cb

        if not self.propname and not self.cb:
            raise RuntimeError(
                "programming error: propname or cb must be specified.")

        if not self.propname and self.lookup_cb == -1:
            raise RuntimeError("programming error: "
                "cliname=%s propname is None but lookup_cb is not specified. "
                "Even if a 'cb' is passed, 'propname' is still used for "
                "device lookup for virt-xml --edit.\n\nIf cb is just "
                "a convertor function for a single propname, then set "
                "both propname and cb. If this cliname is truly "
                "not backed by a single propname, set lookup_cb=None or "
                "better yet implement a lookup_cb. This message is here "
                "to ensure propname isn't omitted without understanding "
                "the distinction." % self.cliname)

        if self.lookup_cb == -1:
            self.lookup_cb = None

    def match_name(self, cliname):
        """
        Return True if the passed argument name matches this
        VirtCLIArgument. So for an option like --foo bar=X, this
        checks if we are the parser for 'bar'
        """
        for argname in [self.cliname] + util.listify(self.aliases):
            if re.match("^%s$" % argname, cliname):
                return True
        return False


class _VirtCLIArgument(object):
    """
    A class that combines the static parsing data _VirtCLIArgumentStatic
    with actual values passed on the command line.
    """

    def __init__(self, virtarg, key, val):
        """
        Instantiate a VirtCLIArgument with the actual key=val pair
        from the command line.
        """
        if val is None:
            # When a command line tuple option has no value set, say
            #   --network bridge=br0,model=virtio
            # is instead called
            #   --network bridge=br0,model
            # We error that 'model' didn't have a value
            raise RuntimeError("Option '%s' had no value set." % key)
        if val == "":
            val = None
        if virtarg.is_onoff:
            val = _on_off_convert(key, val)

        self.val = val
        self.key = key
        self._virtarg = virtarg

        # For convenience
        self.propname = virtarg.propname
        self.cliname = virtarg.cliname

    def parse_param(self, parser, inst):
        """
        Process the cli param against the pass inst.

        So if we are VirtCLIArgument for --disk device=, and the user
        specified --disk device=foo, we were instantiated with
        key=device val=foo, so set inst.device = foo
        """
        if self.val == "default" and self._virtarg.ignore_default:
            return

        if self._virtarg.find_inst_cb:
            inst = self._virtarg.find_inst_cb(parser,
                                              inst, self.val, self, True)

        try:
            if self.propname:
                util.get_prop_path(inst, self.propname)
        except AttributeError:
            raise RuntimeError("programming error: obj=%s does not have "
                               "member=%s" % (inst, self.propname))

        if self._virtarg.cb:
            self._virtarg.cb(parser, inst, self.val, self)
        else:
            util.set_prop_path(inst, self.propname, self.val)

    def lookup_param(self, parser, inst):
        """
        See if the passed value matches our Argument, like via virt-xml

        So if this Argument is for --disk device=, and the user
        specified virt-xml --edit device=floppy --disk ..., we were
        instantiated with key=device val=floppy, so return
        'inst.device == floppy'
        """
        if not self.propname and not self._virtarg.lookup_cb:
            raise RuntimeError(
                _("Don't know how to match device type '%(device_type)s' "
                  "property '%(property_name)s'") %
                {"device_type": getattr(inst, "DEVICE_TYPE", ""),
                 "property_name": self.key})

        if self._virtarg.find_inst_cb:
            inst = self._virtarg.find_inst_cb(parser,
                                              inst, self.val, self, False)
            if not inst:
                return False

        if self._virtarg.lookup_cb:
            return self._virtarg.lookup_cb(parser,
                                           inst, self.val, self)
        else:
            return util.get_prop_path(inst, self.propname) == self.val


def parse_optstr_tuples(optstr):
    """
    Parse the command string into an ordered list of tuples. So
    a string like --disk path=foo,size=5,path=bar will end up like

    [("path", "foo"), ("size", "5"), ("path", "bar")]
    """
    argsplitter = shlex.shlex(optstr or "", posix=True)
    argsplitter.commenters = ""
    argsplitter.whitespace = ","
    argsplitter.whitespace_split = True
    ret = []

    for opt in list(argsplitter):
        if not opt:
            continue

        if "=" in opt:
            cliname, val = opt.split("=", 1)
        else:
            cliname = opt
            val = None

        ret.append((cliname, val))
    return ret


def _parse_optstr_to_dict(optstr, virtargs, remove_first):
    """
    Parse the passed argument string into an OrderedDict WRT
    the passed list of VirtCLIArguments and their special handling.

    So for --disk path=foo,size=5, optstr is 'path=foo,size=5', and
    we return {"path": "foo", "size": "5"}
    """
    optdict = collections.OrderedDict()
    opttuples = parse_optstr_tuples(optstr)

    def _lookup_virtarg(cliname):
        for virtarg in virtargs:
            if virtarg.match_name(cliname):
                return virtarg

    def _consume_comma_arg(commaopt):
        while opttuples:
            cliname, val = opttuples[0]
            if _lookup_virtarg(cliname):
                # Next tuple is for an actual virtarg
                break

            # Next tuple is a continuation of the comma argument,
            # sum it up
            opttuples.pop(0)
            commaopt[1] += "," + cliname
            if val:
                commaopt[1] += "=" + val

        return commaopt

    # Splice in remove_first names upfront
    for idx, (cliname, val) in enumerate(opttuples):
        if val is not None or not remove_first:
            break
        opttuples[idx] = (remove_first.pop(0), cliname)

    while opttuples:
        cliname, val = opttuples.pop(0)
        virtarg = _lookup_virtarg(cliname)
        if not virtarg:
            optdict[cliname] = val
            continue

        if virtarg.can_comma:
            commaopt = _consume_comma_arg([cliname, val])
            cliname = commaopt[0]
            val = commaopt[1]

        optdict[cliname] = val

    return optdict


class _InitClass(type):
    """Metaclass for providing the _init_class function.

    This allows the customisation of class creation. Similar to
    '__init_subclass__' (see https://www.python.org/dev/peps/pep-0487/),
    but without giving us an explicit dep on python 3.6

    """
    def __new__(cls, *args, **kwargs):
        if len(args) != 3:
            return super().__new__(cls, *args)
        name, bases, ns = args
        init = ns.get('_init_class')
        if isinstance(init, types.FunctionType):
            raise RuntimeError("_init_class must be a @classmethod")
        self = super().__new__(cls, name, bases, ns)
        self._init_class(**kwargs)  # pylint: disable=protected-access
        return self


class VirtCLIParser(metaclass=_InitClass):
    """
    Parse a compound arg string like --option foo=bar,baz=12. This is
    the desired interface to VirtCLIArgument and VirtCLIOptionString.

    A command line argument like --disk just extends this interface
    and calls add_arg a bunch to register subarguments like path=,
    size=, etc. See existing impls examples of how to do all sorts of
    crazy stuff.

    Class parameters:
    @guest_propname: The property name in the Guest class that tracks
        the object type that backs this parser. For example, the --sound
        option maps to DeviceSound, which on the guest class is at
        guest.devices.sound, so guest_propname = "devices.sound"
    @remove_first: List of parameters to peel off the front of the
        option string, and store in the optdict. So:
        remove_first=["char_type"] for --serial pty,foo=bar
        maps to {"char_type", "pty", "foo": "bar"}
    @stub_none: If the parsed option string is just 'none', make it a no-op.
        This helps us be backwards compatible: for example, --rng none is
        a no-op, but one day we decide to add an rng device by default to
        certain VMs, and --rng none is extended to handle that. --rng none
        can be added to users command lines and it will give the expected
        results regardless of the virt-install version.
    @cli_arg_name: The command line argument this maps to, so
        "hostdev" for --hostdev
    """
    guest_propname = None
    remove_first = None
    stub_none = True
    cli_arg_name = None
    _virtargs = []

    @classmethod
    def add_arg(cls, *args, **kwargs):
        """
        Add a VirtCLIArgument for this class.
        """
        if not cls._virtargs:
            cls._virtargs = [_VirtCLIArgumentStatic(
                "clearxml", None, cb=cls._clearxml_cb, lookup_cb=None,
                is_onoff=True)]
        cls._virtargs.append(_VirtCLIArgumentStatic(*args, **kwargs))

    @classmethod
    def cli_flag_name(cls):
        return "--" + cls.cli_arg_name.replace("_", "-")

    @classmethod
    def print_introspection(cls):
        """
        Print out all _param names, triggered via ex. --disk help
        """
        def _sortkey(virtarg):
            prefix = ""
            if virtarg.cliname == "clearxml":
                prefix = "0"
            if virtarg.cliname.startswith("address."):
                prefix = "1"
            return prefix + virtarg.cliname

        print("%s options:" % cls.cli_flag_name())
        for arg in sorted(cls._virtargs, key=_sortkey):
            print("  %s" % arg.cliname)
        print("")

    @classmethod
    def lookup_prop(cls, obj):
        """
        For the passed obj, return the equivalent of
        getattr(obj, cls.guest_propname), but handle '.' in the guest_propname
        """
        if not cls.guest_propname:
            return None
        return util.get_prop_path(obj, cls.guest_propname)

    @classmethod
    def prop_is_list(cls, obj):
        inst = cls.lookup_prop(obj)
        return isinstance(inst, list)

    @classmethod
    def register(cls):
        # register the parser class only once
        if cls not in VIRT_PARSERS:
            VIRT_PARSERS.append(cls)

    @classmethod
    def _init_class(cls, **kwargs):
        """This method just terminates the super() chain"""

    def __init__(self, optstr, guest=None):
        self.optstr = optstr
        self.guest = guest
        self.optdict = _parse_optstr_to_dict(self.optstr,
                self._virtargs, util.listify(self.remove_first)[:])

    def _clearxml_cb(self, inst, val, virtarg):
        """
        Callback that handles virt-xml clearxml=yes|no magic
        """
        if not self.guest_propname:
            raise RuntimeError("Don't know how to clearxml for %s" %
                               self.cli_flag_name())
        if val is not True:
            return

        # If there's any opts remaining, leave the root stub element
        # in place with leave_stub=True, so virt-xml updates are done
        # in place.
        #
        # Example: --edit --cpu clearxml=yes should remove the <cpu>
        # block. But --edit --cpu clearxml=yes,model=foo should leave
        # a <cpu> stub in place, so that it gets model=foo in place,
        # otherwise the newly created cpu block gets appended to the
        # end of the domain XML, which gives an ugly diff
        inst.clear(leave_stub=("," in self.optstr))

    def _make_find_inst_cb(self, cliarg, list_propname):
        """
        Create a callback used for find_inst_cb command line lookup.

        :param cliarg: The cliarg string that is followed by an index.
            Example, for --disk seclabel[0-9]* mapping, this is 'seclabel'
        :param list_propname: The property name on the virtinst object that
            this parameter maps too. For the seclabel example, we want
            disk.seclabels, so this value is 'seclabels'
        """
        def cb(inst, val, virtarg, can_edit):
            ignore = val
            num = 0
            reg = re.search(r"%s(\d+)" % cliarg, virtarg.key)
            if reg:
                num = int(reg.groups()[0])

            if can_edit:
                while len(getattr(inst, list_propname)) < (num + 1):
                    getattr(inst, list_propname).add_new()
            try:
                return getattr(inst, list_propname)[num]
            except IndexError:
                if not can_edit:
                    return None
                raise
        return cb

    def _optdict_to_param_list(self, optdict):
        """
        Convert the passed optdict to a list of instantiated
        VirtCLIArguments to actually interact with
        """
        ret = []
        for virtargstatic in self._virtargs:
            for key in list(optdict.keys()):
                if virtargstatic.match_name(key):
                    arginst = _VirtCLIArgument(virtargstatic,
                                               key, optdict.pop(key))
                    ret.append(arginst)
        return ret

    def _check_leftover_opts(self, optdict):
        """
        Used to check if there were any unprocessed entries in the
        optdict after we should have emptied it. Like if the user
        passed an invalid argument such as --disk idontexist=foo
        """
        if optdict:
            fail(_("Unknown %s options: %s") %
                    (self.cli_flag_name(), list(optdict.keys())))

    def _parse(self, inst):
        """
        Subclasses can hook into this to do any pre/post processing
        of the inst, or self.optdict
        """
        optdict = self.optdict.copy()
        for param in self._optdict_to_param_list(optdict):
            param.parse_param(self, inst)

        self._check_leftover_opts(optdict)
        return inst

    def parse(self, inst, validate=True):
        """
        Main entry point. Iterate over self._virtargs, and serialize
        self.optdict into 'inst'.

        For virt-xml, 'inst' is the virtinst object we are editing,
        ex. a DeviceDisk from a parsed Guest object.
        For virt-install, 'inst' is None, and we will create a new
        inst for self.guest_propname, or edit a singleton object in place
        like Guest.features/DomainFeatures
        """
        if not self.optstr:
            return None
        if self.stub_none and self.optstr == "none":
            return None

        new_object = False
        if self.guest_propname and not inst:
            inst = self.lookup_prop(self.guest)
            new_object = self.prop_is_list(self.guest)
            if new_object:
                inst = inst.new()

        ret = []
        try:
            objs = self._parse(inst is None and self.guest or inst)
            if new_object:
                for obj in util.listify(objs):
                    if validate:
                        obj.validate()

                    if isinstance(obj, Device):
                        self.guest.add_device(obj)
                    else:
                        self.guest.add_child(obj)

            ret += util.listify(objs)
        except Exception as e:
            logging.debug("Exception parsing inst=%s optstr=%s",
                          inst, self.optstr, exc_info=True)
            fail(_("Error: %(cli_flag_name)s %(options)s: %(err)s") %
                    {"cli_flag_name": self.cli_flag_name(),
                     "options": self.optstr, "err": str(e)})

        return ret

    def lookup_child_from_option_string(self):
        """
        Given a passed option string, search the guests' child list
        for all objects which match the passed options.

        Used only by virt-xml --edit lookups
        """
        ret = []
        objlist = util.listify(self.lookup_prop(self.guest))

        try:
            for inst in objlist:
                optdict = self.optdict.copy()
                valid = True
                for param in self._optdict_to_param_list(optdict):
                    paramret = param.lookup_param(self, inst)
                    if paramret is False:
                        valid = False
                        break
                if valid:
                    ret.append(inst)
                self._check_leftover_opts(optdict)
        except Exception as e:
            logging.debug("Exception parsing inst=%s optstr=%s",
                          inst, self.optstr, exc_info=True)
            fail(_("Error: %(cli_flag_name)s %(options)s: %(err)s") %
                    {"cli_flag_name": self.cli_flag_name(),
                     "options": self.optstr, "err": str(e)})

        return ret

    def noset_cb(self, inst, val, virtarg):
        """Do nothing callback"""


########################
# --unattended parsing #
########################

class ParserUnattended(VirtCLIParser):
    cli_arg_name = "unattended"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("profile", "profile")
        cls.add_arg("admin-password", "admin_password")
        cls.add_arg("user-password", "user_password")
        cls.add_arg("product-key", "product_key")


def parse_unattended(optstr):
    ret = UnattendedData()
    parser = ParserUnattended(optstr)
    parser.parse(ret)
    return ret


###################
# --check parsing #
###################

def convert_old_force(options):
    if options.force:
        if not options.check:
            options.check = "all=off"
        del(options.force)


class ParserCheck(VirtCLIParser):
    cli_arg_name = "check"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("path_in_use", None, is_onoff=True,
                    cb=cls.set_cb, lookup_cb=None)
        cls.add_arg("disk_size", None, is_onoff=True,
                    cb=cls.set_cb, lookup_cb=None)
        cls.add_arg("path_exists", None, is_onoff=True,
                    cb=cls.set_cb, lookup_cb=None)
        cls.add_arg("all", "all_checks", is_onoff=True)

    def set_cb(self, inst, val, virtarg):
        # This sets properties on the _GlobalState objects
        inst.set_validation_check(virtarg.cliname, val)


def parse_check(checks):
    # Overwrite this for each parse
    for optstr in util.listify(checks):
        parser = ParserCheck(optstr)
        parser.parse(get_global_state())


######################
# --location parsing #
######################

class ParserLocation(VirtCLIParser):
    cli_arg_name = "location"
    remove_first = "location"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("location", "location", can_comma=True)
        cls.add_arg("kernel", "kernel", can_comma=True)
        cls.add_arg("initrd", "initrd", can_comma=True)


def parse_location(optstr):
    class LocationData:
        def __init__(self):
            self.location = None
            self.kernel = None
            self.initrd = None
    parsedata = LocationData()
    parser = ParserLocation(optstr or None)
    parser.parse(parsedata)

    return parsedata.location, parsedata.kernel, parsedata.initrd


########################
# --os-variant parsing #
########################

class OSVariantData(object):
    def __init__(self):
        self._name = None
        self.full_id = None
        self.is_none = False
        self.is_auto = False
        self.install = None

    def _set_name(self, val):
        if val == "auto":
            self.is_auto = True
        elif val == "none":
            self.is_none = True
        else:
            self._name = val
    def _get_name(self):
        return self._name
    name = property(_get_name, _set_name)

    def set_os_name(self, guest):
        if self.full_id:
            guest.set_os_full_id(self.full_id)
        elif self.name:
            guest.set_os_name(self.name)


class ParserOSVariant(VirtCLIParser):
    cli_arg_name = "os_variant"
    remove_first = "name"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("name", "name")
        cls.add_arg("full_id", "full_id")
        cls.add_arg("install", "install")


def parse_os_variant(optstr):
    parsedata = OSVariantData()
    if optstr:
        parser = ParserOSVariant(optstr)
        parser.parse(parsedata)
    return parsedata


######################
# --metadata parsing #
######################

class ParserMetadata(VirtCLIParser):
    cli_arg_name = "metadata"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("name", "name", can_comma=True)
        cls.add_arg("title", "title", can_comma=True)
        cls.add_arg("uuid", "uuid")
        cls.add_arg("description", "description", can_comma=True)
        cls.add_arg("os_name", None, lookup_cb=None,
                cb=cls.set_os_name_cb)
        cls.add_arg("os_full_id", None, lookup_cb=None,
                cb=cls.set_os_full_id_cb)

    def set_os_name_cb(self, inst, val, virtarg):
        inst.set_os_name(val)

    def set_os_full_id_cb(self, inst, val, virtarg):
        inst.set_os_full_id(val)


####################
# --events parsing #
####################

class ParserEvents(VirtCLIParser):
    cli_arg_name = "events"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("on_poweroff", "on_poweroff")
        cls.add_arg("on_reboot", "on_reboot")
        cls.add_arg("on_crash", "on_crash")
        cls.add_arg("on_lockfailure", "on_lockfailure")


######################
# --resource parsing #
######################

class ParserResource(VirtCLIParser):
    cli_arg_name = "resource"
    guest_propname = "resource"
    remove_first = "partition"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("partition", "partition")


######################
# --numatune parsing #
######################

class ParserNumatune(VirtCLIParser):
    cli_arg_name = "numatune"
    guest_propname = "numatune"
    remove_first = "nodeset"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("nodeset", "memory_nodeset", can_comma=True)
        cls.add_arg("mode", "memory_mode")


####################
# --memory parsing #
####################

class ParserMemory(VirtCLIParser):
    cli_arg_name = "memory"
    remove_first = "memory"

    def set_memory_cb(self, inst, val, virtarg):
        util.set_prop_path(inst, virtarg.cliname, int(val) * 1024)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("memory", "memory", cb=cls.set_memory_cb)
        cls.add_arg("maxmemory", "maxmemory", cb=cls.set_memory_cb)
        cls.add_arg("hugepages", "memoryBacking.hugepages", is_onoff=True)
        cls.add_arg("hotplugmemorymax", "hotplugmemorymax",
                    cb=cls.set_memory_cb)
        cls.add_arg("hotplugmemoryslots", "hotplugmemoryslots")


#####################
# --memtune parsing #
#####################

class ParserMemtune(VirtCLIParser):
    cli_arg_name = "memtune"
    guest_propname = "memtune"
    remove_first = "soft_limit"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("hard_limit", "hard_limit")
        cls.add_arg("soft_limit", "soft_limit")
        cls.add_arg("swap_hard_limit", "swap_hard_limit")
        cls.add_arg("min_guarantee", "min_guarantee")


#######################
# --blkiotune parsing #
#######################

class ParserBlkiotune(VirtCLIParser):
    cli_arg_name = "blkiotune"
    guest_propname = "blkiotune"
    remove_first = "weight"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("weight", "weight")
        cls.add_arg("device_path", "device_path")
        cls.add_arg("device_weight", "device_weight")


###########################
# --memorybacking parsing #
###########################

class ParserMemoryBacking(VirtCLIParser):
    cli_arg_name = "memorybacking"
    guest_propname = "memoryBacking"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("hugepages", "hugepages", is_onoff=True)
        cls.add_arg("size", "page_size")
        cls.add_arg("unit", "page_unit")
        cls.add_arg("nodeset", "page_nodeset", can_comma=True)
        cls.add_arg("nosharepages", "nosharepages", is_onoff=True)
        cls.add_arg("locked", "locked", is_onoff=True)
        cls.add_arg("access_mode", "access_mode")
        cls.add_arg("source_type", "source_type")


#################
# --cpu parsing #
#################

class ParserCPU(VirtCLIParser):
    cli_arg_name = "cpu"
    guest_propname = "cpu"
    remove_first = "model"
    stub_none = False

    def cell_find_inst_cb(self, *args, **kwargs):
        cliarg = "cell"  # cell[0-9]*
        list_propname = "cells"  # cpu.cells
        cb = self._make_find_inst_cb(cliarg, list_propname)
        return cb(*args, **kwargs)

    def sibling_find_inst_cb(self, inst, *args, **kwargs):
        cell = self.cell_find_inst_cb(inst, *args, **kwargs)
        inst = cell

        cliarg = "sibling"  # cell[0-9]*.distances.sibling[0-9]*
        list_propname = "siblings"  # cell.siblings
        cb = self._make_find_inst_cb(cliarg, list_propname)
        return cb(inst, *args, **kwargs)

    def set_model_cb(self, inst, val, virtarg):
        if val == "host":
            val = inst.SPECIAL_MODE_HOST_MODEL
        if val == "none":
            val = inst.SPECIAL_MODE_CLEAR

        if val in inst.SPECIAL_MODES:
            inst.set_special_mode(self.guest, val)
        else:
            inst.set_model(self.guest, val)

    def set_feature_cb(self, inst, val, virtarg):
        policy = virtarg.cliname
        for feature_name in util.listify(val):
            featureobj = None

            for f in inst.features:
                if f.name == feature_name:
                    featureobj = f
                    break

            if featureobj:
                featureobj.policy = policy
            else:
                inst.add_feature(feature_name, policy)

    def set_l3_cache_cb(self, inst, val, virtarg, can_edit):
        cpu = inst

        if can_edit and not cpu.cache:
            cpu.cache.add_new()
        try:
            return cpu.cache[0]
        except IndexError:
            if not can_edit:
                return None
            raise

    def _parse(self, inst):
        # For old CLI compat, --cpu force=foo,force=bar should force
        # enable 'foo' and 'bar' features, but that doesn't fit with the
        # CLI parser infrastructure very well.
        converted = collections.defaultdict(list)
        for key, value in parse_optstr_tuples(self.optstr):
            if key in ["force", "require", "optional", "disable", "forbid"]:
                converted[key].append(value)

        # Convert +feature, -feature into expected format
        for key, value in list(self.optdict.items()):
            policy = None
            if value or len(key) == 1:
                continue

            if key.startswith("+"):
                policy = "force"
            elif key.startswith("-"):
                policy = "disable"

            if policy:
                del(self.optdict[key])
                converted[policy].append(key[1:])

        self.optdict.update(converted)
        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("model", "model", cb=cls.set_model_cb)
        cls.add_arg("mode", "mode")
        cls.add_arg("match", "match")
        cls.add_arg("vendor", "vendor")
        cls.add_arg("secure", "secure", is_onoff=True)

        # These are handled specially in _parse
        cls.add_arg("force", None, lookup_cb=None, cb=cls.set_feature_cb)
        cls.add_arg("require", None, lookup_cb=None, cb=cls.set_feature_cb)
        cls.add_arg("optional", None, lookup_cb=None, cb=cls.set_feature_cb)
        cls.add_arg("disable", None, lookup_cb=None, cb=cls.set_feature_cb)
        cls.add_arg("forbid", None, lookup_cb=None, cb=cls.set_feature_cb)

        # Options for CPU.cells config
        cls.add_arg("cell[0-9]*.id", "id",
                    find_inst_cb=cls.cell_find_inst_cb)
        cls.add_arg("cell[0-9]*.cpus", "cpus", can_comma=True,
                    find_inst_cb=cls.cell_find_inst_cb)
        cls.add_arg("cell[0-9]*.memory", "memory",
                    find_inst_cb=cls.cell_find_inst_cb)
        cls.add_arg("cell[0-9]*.distances.sibling[0-9]*.id", "id",
                    find_inst_cb=cls.sibling_find_inst_cb)
        cls.add_arg("cell[0-9]*.distances.sibling[0-9]*.value", "value",
                    find_inst_cb=cls.sibling_find_inst_cb)

        # Options for CPU.cache
        cls.add_arg("cache.mode", "mode", find_inst_cb=cls.set_l3_cache_cb)
        cls.add_arg("cache.level", "level", find_inst_cb=cls.set_l3_cache_cb)


#####################
# --cputune parsing #
#####################

class ParserCputune(VirtCLIParser):
    cli_arg_name = "cputune"
    guest_propname = "cputune"
    remove_first = "model"
    stub_none = False

    def vcpu_find_inst_cb(self, *args, **kwargs):
        cliarg = "vcpupin"  # vcpupin[0-9]*
        list_propname = "vcpus"
        cb = self._make_find_inst_cb(cliarg, list_propname)
        return cb(*args, **kwargs)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        # Options for CPU.vcpus config
        cls.add_arg("vcpupin[0-9]*.vcpu", "vcpu",
                    find_inst_cb=cls.vcpu_find_inst_cb)
        cls.add_arg("vcpupin[0-9]*.cpuset", "cpuset", can_comma=True,
                    find_inst_cb=cls.vcpu_find_inst_cb)


###################
# --vcpus parsing #
###################

class ParserVCPU(VirtCLIParser):
    cli_arg_name = "vcpus"
    remove_first = "vcpus"

    def set_vcpus_cb(self, inst, val, virtarg):
        propname = (("maxvcpus" in self.optdict) and
                    "curvcpus" or "vcpus")
        util.set_prop_path(inst, propname, val)

    def set_cpuset_cb(self, inst, val, virtarg):
        if not val:
            return
        if val != "auto":
            inst.cpuset = val
            return

        # Previously we did our own one-time cpuset placement
        # based on current NUMA memory availability, but that's
        # pretty dumb unless the conditions on the host never change.
        # So instead use newer vcpu placement=
        inst.vcpu_placement = "auto"

    def _parse(self, inst):
        set_from_top = ("maxvcpus" not in self.optdict and
                        "vcpus" not in self.optdict)

        ret = super()._parse(inst)

        if set_from_top:
            inst.vcpus = inst.cpu.vcpus_from_topology()
        return ret

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("sockets", "cpu.sockets")
        cls.add_arg("cores", "cpu.cores")
        cls.add_arg("threads", "cpu.threads")

        cls.add_arg("vcpus", "curvcpus", cb=cls.set_vcpus_cb)
        cls.add_arg("maxvcpus", "vcpus")

        cls.add_arg("cpuset", "cpuset", can_comma=True, cb=cls.set_cpuset_cb)
        cls.add_arg("placement", "vcpu_placement")


##################
# --boot parsing #
##################

class ParserBoot(VirtCLIParser):
    cli_arg_name = "boot"
    guest_propname = "os"

    def set_uefi_cb(self, inst, val, virtarg):
        self.guest.set_uefi_path(self.guest.get_uefi_path())

    def set_initargs_cb(self, inst, val, virtarg):
        inst.set_initargs_string(val)

    def set_smbios_mode_cb(self, inst, val, virtarg):
        inst.smbios_mode = val
        self.optdict["smbios_mode"] = val

    def set_bootloader_cb(self, inst, val, virtarg):
        self.guest.bootloader = val

    def set_domain_type_cb(self, inst, val, virtarg):
        self.guest.type = val

    def set_emulator_cb(self, inst, val, virtarg):
        self.guest.emulator = val

    def _parse(self, inst):
        # Build boot order
        boot_order = []
        for cliname in list(self.optdict.keys()):
            if cliname not in inst.BOOT_DEVICES:
                continue

            del(self.optdict[cliname])
            if cliname not in boot_order:
                boot_order.append(cliname)

        if boot_order:
            inst.bootorder = boot_order

        # Back compat to allow uefi to have no cli value specified
        if "uefi" in self.optdict:
            self.optdict["uefi"] = True

        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        # UEFI depends on these bits, so set them first
        cls.add_arg("arch", "arch")
        cls.add_arg("bootloader", None, lookup_cb=None,
                cb=cls.set_bootloader_cb)
        cls.add_arg("domain_type", None, lookup_cb=None,
                cb=cls.set_domain_type_cb)
        cls.add_arg("emulator", None, lookup_cb=None,
                cb=cls.set_emulator_cb)
        cls.add_arg("uefi", None, lookup_cb=None,
                cb=cls.set_uefi_cb)
        cls.add_arg("os_type", "os_type")
        cls.add_arg("machine", "machine")

        cls.add_arg("useserial", "useserial", is_onoff=True)
        cls.add_arg("menu", "enable_bootmenu", is_onoff=True)
        cls.add_arg("rebootTimeout", "rebootTimeout")
        cls.add_arg("kernel", "kernel")
        cls.add_arg("initrd", "initrd")
        cls.add_arg("dtb", "dtb")
        cls.add_arg("loader", "loader")
        cls.add_arg("loader_ro", "loader_ro", is_onoff=True)
        cls.add_arg("loader_type", "loader_type")
        cls.add_arg("loader_secure", "loader_secure", is_onoff=True)
        cls.add_arg("nvram", "nvram")
        cls.add_arg("nvram_template", "nvram_template")
        cls.add_arg("kernel_args", "kernel_args",
                           aliases=["extra_args"], can_comma=True)
        cls.add_arg("init", "init")
        cls.add_arg("initargs", "initargs", cb=cls.set_initargs_cb)
        cls.add_arg("smbios_mode", "smbios_mode")

        # This is simply so the boot options are advertised with --boot help,
        # actual processing is handled by _parse
        for _bootdev in DomainOs.BOOT_DEVICES:
            cls.add_arg(_bootdev, None, lookup_cb=None,
                    cb=cls.noset_cb)


###################
# --idmap parsing #
###################

class ParserIdmap(VirtCLIParser):
    cli_arg_name = "idmap"
    guest_propname = "idmap"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("uid_start", "uid_start")
        cls.add_arg("uid_target", "uid_target")
        cls.add_arg("uid_count", "uid_count")
        cls.add_arg("gid_start", "gid_start")
        cls.add_arg("gid_target", "gid_target")
        cls.add_arg("gid_count", "gid_count")


######################
# --security parsing #
######################

class ParserSecurity(VirtCLIParser):
    cli_arg_name = "security"
    guest_propname = "seclabels"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("type", "type")
        cls.add_arg("model", "model")
        cls.add_arg("relabel", "relabel", is_onoff=True)
        cls.add_arg("label", "label", can_comma=True)
        cls.add_arg("baselabel", "baselabel", can_comma=True)


######################
# --features parsing #
######################

class ParserFeatures(VirtCLIParser):
    cli_arg_name = "features"
    guest_propname = "features"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("acpi", "acpi", is_onoff=True)
        cls.add_arg("apic", "apic", is_onoff=True)
        cls.add_arg("pae", "pae", is_onoff=True)
        cls.add_arg("privnet", "privnet", is_onoff=True)
        cls.add_arg("hap", "hap", is_onoff=True)
        cls.add_arg("viridian", "viridian", is_onoff=True)
        cls.add_arg("eoi", "eoi", is_onoff=True)
        cls.add_arg("pmu", "pmu", is_onoff=True)

        cls.add_arg("hyperv_reset", "hyperv_reset", is_onoff=True)
        cls.add_arg("hyperv_vapic", "hyperv_vapic", is_onoff=True)
        cls.add_arg("hyperv_relaxed", "hyperv_relaxed", is_onoff=True)
        cls.add_arg("hyperv_spinlocks", "hyperv_spinlocks", is_onoff=True)
        cls.add_arg("hyperv_spinlocks_retries", "hyperv_spinlocks_retries")
        cls.add_arg("hyperv_synic", "hyperv_synic", is_onoff=True)

        cls.add_arg("vmport", "vmport", is_onoff=True)
        cls.add_arg("kvm_hidden", "kvm_hidden", is_onoff=True)
        cls.add_arg("pvspinlock", "pvspinlock", is_onoff=True)

        cls.add_arg("gic_version", "gic_version")

        cls.add_arg("smm", "smm", is_onoff=True)
        cls.add_arg("vmcoreinfo", "vmcoreinfo", is_onoff=True)


###################
# --clock parsing #
###################

class ParserClock(VirtCLIParser):
    cli_arg_name = "clock"
    guest_propname = "clock"

    def set_timer(self, inst, val, virtarg):
        tname, propname = virtarg.cliname.split("_")

        timerobj = None
        for t in inst.timers:
            if t.name == tname:
                timerobj = t
                break

        if not timerobj:
            timerobj = inst.timers.add_new()
            timerobj.name = tname

        util.set_prop_path(timerobj, propname, val)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("offset", "offset")

        for _tname in DomainClock.TIMER_NAMES:
            cls.add_arg(_tname + "_present", None, lookup_cb=None,
                    is_onoff=True,
                    cb=cls.set_timer)
            cls.add_arg(_tname + "_tickpolicy", None, lookup_cb=None,
                    cb=cls.set_timer)


################
# --pm parsing #
################

class ParserPM(VirtCLIParser):
    cli_arg_name = "pm"
    guest_propname = "pm"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("suspend_to_mem", "suspend_to_mem", is_onoff=True)
        cls.add_arg("suspend_to_disk", "suspend_to_disk", is_onoff=True)


#####################
# --sysinfo parsing #
#####################

class ParserSysinfo(VirtCLIParser):
    cli_arg_name = "sysinfo"
    guest_propname = "sysinfo"
    remove_first = "type"

    def set_type_cb(self, inst, val, virtarg):
        if val == "host" or val == "emulate":
            self.guest.os.smbios_mode = val
        elif val == "smbios":
            self.guest.os.smbios_mode = "sysinfo"
            inst.type = val
        else:
            fail(_("Unknown sysinfo flag '%s'") % val)

    def set_uuid_cb(self, inst, val, virtarg):
        # If a uuid is supplied it must match the guest UUID. This would be
        # impossible to guess if the guest uuid is autogenerated so just
        # overwrite the guest uuid with what is passed in assuming it passes
        # the sanity checking below.
        inst.system_uuid = val
        self.guest.uuid = val

    def _parse(self, inst):
        if self.optstr == "host" or self.optstr == "emulate":
            self.optdict['type'] = self.optstr
        elif self.optstr:
            # If any string specified, default to type=smbios otherwise
            # libvirt errors. User args can still override this though
            self.optdict['type'] = 'smbios'

        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        # <sysinfo type='smbios'>
        cls.add_arg("type", "type",
                              cb=cls.set_type_cb, can_comma=True)

        # <bios> type 0 BIOS Information
        cls.add_arg("bios_vendor", "bios_vendor")
        cls.add_arg("bios_version", "bios_version")
        cls.add_arg("bios_date", "bios_date")
        cls.add_arg("bios_release", "bios_release")

        # <system> type 1 System Information
        cls.add_arg("system_manufacturer", "system_manufacturer")
        cls.add_arg("system_product", "system_product")
        cls.add_arg("system_version", "system_version")
        cls.add_arg("system_serial", "system_serial")
        cls.add_arg("system_uuid", "system_uuid",
                              cb=cls.set_uuid_cb)
        cls.add_arg("system_sku", "system_sku")
        cls.add_arg("system_family", "system_family")

        # <baseBoard> type 2 Baseboard (or Module) Information
        cls.add_arg("baseBoard_manufacturer", "baseBoard_manufacturer")
        cls.add_arg("baseBoard_product", "baseBoard_product")
        cls.add_arg("baseBoard_version", "baseBoard_version")
        cls.add_arg("baseBoard_serial", "baseBoard_serial")
        cls.add_arg("baseBoard_asset", "baseBoard_asset")
        cls.add_arg("baseBoard_location", "baseBoard_location")


##############################
# --qemu-commandline parsing #
##############################

class ParserQemuCLI(VirtCLIParser):
    cli_arg_name = "qemu_commandline"
    guest_propname = "xmlns_qemu"

    def args_cb(self, inst, val, virtarg):
        for opt in shlex.split(val):
            obj = inst.args.add_new()
            obj.value = opt

    def env_cb(self, inst, val, virtarg):
        name, envval = val.split("=", 1)
        obj = inst.envs.add_new()
        obj.name = name
        obj.value = envval

    def _parse(self, inst):
        self.optdict.clear()
        if self.optstr.startswith("env="):
            self.optdict["env"] = self.optstr.split("=", 1)[1]
        elif self.optstr.startswith("args="):
            self.optdict["args"] = self.optstr.split("=", 1)[1]
        elif self.optstr.startswith("clearxml="):
            self.optdict["clearxml"] = self.optstr.split("=", 1)[1]
        else:
            self.optdict["args"] = self.optstr
        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("args", None, lookup_cb=None,
                cb=cls.args_cb, can_comma=True)
        cls.add_arg("env", None, lookup_cb=None,
                cb=cls.env_cb, can_comma=True)


##########################
# Guest <device> parsing #
##########################

def _add_device_address_args(cls):
    """
    Add DeviceAddress parameters if we are parsing for a device
    """
    cls.add_arg("address.type", "address.type")
    cls.add_arg("address.domain", "address.domain")
    cls.add_arg("address.bus", "address.bus")
    cls.add_arg("address.slot", "address.slot")
    cls.add_arg("address.multifunction", "address.multifunction",
                is_onoff=True)
    cls.add_arg("address.function", "address.function")
    cls.add_arg("address.controller", "address.controller")
    cls.add_arg("address.unit", "address.unit")
    cls.add_arg("address.port", "address.port")
    cls.add_arg("address.target", "address.target")
    cls.add_arg("address.reg", "address.reg")
    cls.add_arg("address.cssid", "address.cssid")
    cls.add_arg("address.ssid", "address.ssid")
    cls.add_arg("address.devno", "address.devno")
    cls.add_arg("address.iobase", "address.iobase")
    cls.add_arg("address.irq", "address.irq")
    cls.add_arg("address.base", "address.base")


def _add_device_boot_order_arg(cls):
    def set_boot_order_cb(self, inst, val, virtarg):
        val = int(val)
        self.guest.reorder_boot_order(inst, val)
    cls.set_boot_order_cb = set_boot_order_cb
    cls.add_arg("boot_order", "boot.order", cb=cls.set_boot_order_cb)


##################
# --disk parsing #
##################

def _default_image_file_format(conn):
    if conn.check_support(conn.SUPPORT_CONN_DEFAULT_QCOW2):
        return "qcow2"
    return "raw"


def _get_default_image_format(conn, poolobj):
    tmpvol = StorageVolume(conn)
    tmpvol.pool = poolobj

    if tmpvol.file_type != StorageVolume.TYPE_FILE:
        return None
    return _default_image_file_format(conn)


def _generate_new_volume_name(guest, poolobj, fmt):
    collidelist = []
    for disk in guest.devices.disk:
        if (disk.get_vol_install() and
            disk.get_vol_install().pool.name() == poolobj.name()):
            collidelist.append(os.path.basename(disk.path))

    ext = StorageVolume.get_file_extension_for_format(fmt)
    return StorageVolume.find_free_name(
        poolobj, guest.name, suffix=ext, collidelist=collidelist)


class ParserDisk(VirtCLIParser):
    cli_arg_name = "disk"
    guest_propname = "devices.disk"
    remove_first = "path"
    stub_none = False

    def seclabel_find_inst_cb(self, *args, **kwargs):
        cliarg = "seclabel"  # seclabel[0-9]*
        list_propname = "seclabels"  # disk.seclabels
        cb = self._make_find_inst_cb(cliarg, list_propname)
        return cb(*args, **kwargs)

    def _parse(self, inst):
        if self.optstr == "none":
            return

        def parse_size(val):
            if val is None:
                return None
            try:
                return float(val)
            except Exception as e:
                fail(_("Improper value for 'size': %s") % str(e))

        def convert_perms(val):
            if val is None:
                return
            if val == "ro":
                self.optdict["readonly"] = "on"
            elif val == "sh":
                self.optdict["shareable"] = "on"
            elif val == "rw":
                # It's default. Nothing to do.
                pass
            else:
                fail(_("Unknown '%s' value '%s'") % ("perms", val))

        has_path = "path" in self.optdict
        backing_store = self.optdict.pop("backing_store", None)
        backing_format = self.optdict.pop("backing_format", None)
        poolname = self.optdict.pop("pool", None)
        volname = self.optdict.pop("vol", None)
        size = parse_size(self.optdict.pop("size", None))
        fmt = self.optdict.pop("format", None)
        sparse = _on_off_convert("sparse", self.optdict.pop("sparse", "yes"))
        convert_perms(self.optdict.pop("perms", None))
        has_type_volume = ("source_pool" in self.optdict or
                           "source_volume" in self.optdict)
        has_type_network = ("source_protocol" in self.optdict)

        optcount = sum([bool(p) for p in [has_path, poolname, volname,
                                          has_type_volume, has_type_network]])
        if optcount > 1:
            fail(_("Cannot specify more than 1 storage path"))
        if optcount == 0 and size:
            # Saw something like --disk size=X, have it imply pool=default
            poolname = "default"

        if volname:
            if volname.count("/") != 1:
                raise ValueError(_("Storage volume must be specified as "
                                   "vol=poolname/volname"))
            poolname, volname = volname.split("/")
            logging.debug("Parsed --disk volume as: pool=%s vol=%s",
                          poolname, volname)

        super()._parse(inst)

        # Generate and fill in the disk source info
        newvolname = None
        poolobj = None
        if poolname:
            if poolname == "default":
                poolxml = StoragePool.build_default_pool(self.guest.conn)
                if poolxml:
                    poolname = poolxml.name
            poolobj = self.guest.conn.storagePoolLookupByName(poolname)
            StoragePool.ensure_pool_is_running(poolobj)

        if volname:
            vol_object = poolobj.storageVolLookupByName(volname)
            inst.set_vol_object(vol_object, poolobj)
            poolobj = None

        if ((poolobj or inst.wants_storage_creation()) and
            (fmt or size or sparse or backing_store)):
            if not poolobj:
                poolobj = inst.get_parent_pool()
                newvolname = os.path.basename(inst.path)
            if poolobj and not fmt:
                fmt = _get_default_image_format(self.guest.conn, poolobj)
            if newvolname is None:
                newvolname = _generate_new_volume_name(self.guest, poolobj,
                                                       fmt)
            vol_install = DeviceDisk.build_vol_install(
                    self.guest.conn, newvolname, poolobj, size, sparse,
                    fmt=fmt, backing_store=backing_store,
                    backing_format=backing_format)
            inst.set_vol_install(vol_install)

        return inst

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        # These are all handled specially in _parse
        cls.add_arg("backing_store", None, lookup_cb=None, cb=cls.noset_cb)
        cls.add_arg("backing_format", None, lookup_cb=None, cb=cls.noset_cb)
        cls.add_arg("pool", None, lookup_cb=None, cb=cls.noset_cb)
        cls.add_arg("vol", None, lookup_cb=None, cb=cls.noset_cb)
        cls.add_arg("size", None, lookup_cb=None, cb=cls.noset_cb)
        cls.add_arg("format", None, lookup_cb=None, cb=cls.noset_cb)
        cls.add_arg("sparse", None, lookup_cb=None, cb=cls.noset_cb)

        # More standard XML props
        cls.add_arg("source_pool", "source_pool")
        cls.add_arg("source_volume", "source_volume")
        cls.add_arg("source_name", "source_name")
        cls.add_arg("source_protocol", "source_protocol")
        cls.add_arg("source_host_name", "source_host_name")
        cls.add_arg("source_host_port", "source_host_port")
        cls.add_arg("source_host_socket", "source_host_socket")
        cls.add_arg("source_host_transport", "source_host_transport")

        cls.add_arg("path", "path")
        cls.add_arg("device", "device")
        cls.add_arg("snapshot_policy", "snapshot_policy")
        cls.add_arg("bus", "bus")
        cls.add_arg("removable", "removable", is_onoff=True)
        cls.add_arg("cache", "driver_cache")
        cls.add_arg("discard", "driver_discard")
        cls.add_arg("detect_zeroes", "driver_detect_zeroes")
        cls.add_arg("driver_name", "driver_name")
        cls.add_arg("driver_type", "driver_type")
        cls.add_arg("driver.copy_on_read", "driver_copy_on_read", is_onoff=True)
        cls.add_arg("io", "driver_io")
        cls.add_arg("error_policy", "error_policy")
        cls.add_arg("serial", "serial")
        cls.add_arg("target", "target")
        cls.add_arg("startup_policy", "startup_policy")
        cls.add_arg("readonly", "read_only", is_onoff=True)
        cls.add_arg("shareable", "shareable", is_onoff=True)
        _add_device_boot_order_arg(cls)

        cls.add_arg("read_bytes_sec", "iotune_rbs")
        cls.add_arg("write_bytes_sec", "iotune_wbs")
        cls.add_arg("total_bytes_sec", "iotune_tbs")
        cls.add_arg("read_iops_sec", "iotune_ris")
        cls.add_arg("write_iops_sec", "iotune_wis")
        cls.add_arg("total_iops_sec", "iotune_tis")
        cls.add_arg("sgio", "sgio")
        cls.add_arg("logical_block_size", "logical_block_size")
        cls.add_arg("physical_block_size", "physical_block_size")

        # DeviceDisk.seclabels properties
        cls.add_arg("seclabel[0-9]*.model", "model",
                    find_inst_cb=cls.seclabel_find_inst_cb)
        cls.add_arg("seclabel[0-9]*.relabel", "relabel", is_onoff=True,
                    find_inst_cb=cls.seclabel_find_inst_cb)
        cls.add_arg("seclabel[0-9]*.label", "label", can_comma=True,
                    find_inst_cb=cls.seclabel_find_inst_cb)

        cls.add_arg("geometry.cyls", "geometry_cyls")
        cls.add_arg("geometry.heads", "geometry_heads")
        cls.add_arg("geometry.secs", "geometry_secs")
        cls.add_arg("geometry.trans", "geometry_trans")

        cls.add_arg("reservations.managed", "reservations_managed")
        cls.add_arg("reservations.source.type", "reservations_source_type")
        cls.add_arg("reservations.source.path", "reservations_source_path")
        cls.add_arg("reservations.source.mode", "reservations_source_mode")


#####################
# --network parsing #
#####################

class ParserNetwork(VirtCLIParser):
    cli_arg_name = "network"
    guest_propname = "devices.interface"
    remove_first = "type"
    stub_none = False

    def set_mac_cb(self, inst, val, virtarg):
        if val == "RANDOM":
            return None
        inst.macaddr = val
        return val

    def set_type_cb(self, inst, val, virtarg):
        if val == "default":
            inst.set_default_source()
        else:
            inst.type = val

    def set_link_state(self, inst, val, virtarg):
        ignore = virtarg
        if val in ["up", "down"]:
            inst.link_state = val
            return

        ret = _raw_on_off_convert(val)
        if ret is True:
            val = "up"
        elif ret is False:
            val = "down"
        inst.link_state = val

    def _parse(self, inst):
        if self.optstr == "none":
            return

        if "type" not in self.optdict:
            if "network" in self.optdict:
                self.optdict["type"] = DeviceInterface.TYPE_VIRTUAL
                self.optdict["source"] = self.optdict.pop("network")
            elif "bridge" in self.optdict:
                self.optdict["type"] = DeviceInterface.TYPE_BRIDGE
                self.optdict["source"] = self.optdict.pop("bridge")

        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type", cb=cls.set_type_cb)
        cls.add_arg("trustGuestRxFilters", "trustGuestRxFilters",
                              is_onoff=True)
        cls.add_arg("source", "source")
        cls.add_arg("source_mode", "source_mode")
        cls.add_arg("source_type", "source_type")
        cls.add_arg("source_path", "source_path")
        cls.add_arg("portgroup", "portgroup")
        cls.add_arg("target", "target_dev")
        cls.add_arg("model", "model")
        cls.add_arg("mac", "macaddr", cb=cls.set_mac_cb)
        cls.add_arg("filterref", "filterref")
        _add_device_boot_order_arg(cls)
        cls.add_arg("link_state", "link_state",
                              cb=cls.set_link_state)

        cls.add_arg("driver_name", "driver_name")
        cls.add_arg("driver_queues", "driver_queues")

        cls.add_arg("rom_file", "rom_file")
        cls.add_arg("rom_bar", "rom_bar", is_onoff=True)

        cls.add_arg("mtu.size", "mtu_size")

        # For 802.1Qbg
        cls.add_arg("virtualport_type", "virtualport.type")
        cls.add_arg("virtualport_managerid", "virtualport.managerid")
        cls.add_arg("virtualport_typeid", "virtualport.typeid")
        cls.add_arg("virtualport_typeidversion", "virtualport.typeidversion")
        cls.add_arg("virtualport_instanceid", "virtualport.instanceid")
        # For openvswitch & 802.1Qbh
        cls.add_arg("virtualport_profileid", "virtualport.profileid")
        # For openvswitch & midonet
        cls.add_arg("virtualport_interfaceid", "virtualport.interfaceid")


######################
# --graphics parsing #
######################

class ParserGraphics(VirtCLIParser):
    cli_arg_name = "graphics"
    guest_propname = "devices.graphics"
    remove_first = "type"
    stub_none = False

    def set_keymap_cb(self, inst, val, virtarg):
        from . import hostkeymap

        if not val:
            val = None
        elif val.lower() == "local":
            val = DeviceGraphics.KEYMAP_LOCAL
        elif val.lower() == "none":
            val = None
        else:
            use_keymap = hostkeymap.sanitize_keymap(val)
            if not use_keymap:
                raise ValueError(
                    _("Didn't match keymap '%s' in keytable!") % val)
            val = use_keymap
        inst.keymap = val

    def set_type_cb(self, inst, val, virtarg):
        if val == "default":
            return
        inst.type = val

    def listens_find_inst_cb(self, *args, **kwargs):
        cliarg = "listens"  # listens[0-9]*
        list_propname = "listens"  # graphics.listens
        cb = self._make_find_inst_cb(cliarg, list_propname)
        return cb(*args, **kwargs)

    def _parse(self, inst):
        if self.optstr == "none":
            self.guest.skip_default_graphics = True
            return

        ret = super()._parse(inst)

        if inst.conn.is_qemu() and inst.gl:
            if inst.type != "spice":
                logging.warning("graphics type=%s does not support GL", inst.type)
            elif not inst.conn.check_support(
                    inst.conn.SUPPORT_CONN_SPICE_GL):
                logging.warning("qemu/libvirt version may not support spice GL")
        if inst.conn.is_qemu() and inst.rendernode:
            if inst.type != "spice":
                logging.warning("graphics type=%s does not support rendernode", inst.type)
            elif not inst.conn.check_support(
                    inst.conn.SUPPORT_CONN_SPICE_RENDERNODE):
                logging.warning("qemu/libvirt version may not support rendernode")

        return ret

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type", cb=cls.set_type_cb)
        cls.add_arg("port", "port")
        cls.add_arg("tlsport", "tlsPort")
        cls.add_arg("listen", "listen")
        cls.add_arg("listens[0-9]*.type", "type",
                               find_inst_cb=cls.listens_find_inst_cb)
        cls.add_arg("listens[0-9]*.address", "address",
                               find_inst_cb=cls.listens_find_inst_cb)
        cls.add_arg("listens[0-9]*.network", "network",
                               find_inst_cb=cls.listens_find_inst_cb)
        cls.add_arg("listens[0-9]*.socket", "socket",
                               find_inst_cb=cls.listens_find_inst_cb)
        cls.add_arg("keymap", "keymap", cb=cls.set_keymap_cb)
        cls.add_arg("password", "passwd")
        cls.add_arg("passwordvalidto", "passwdValidTo")
        cls.add_arg("connected", "connected")
        cls.add_arg("defaultMode", "defaultMode")

        cls.add_arg("image_compression", "image_compression")
        cls.add_arg("streaming_mode", "streaming_mode")
        cls.add_arg("clipboard_copypaste", "clipboard_copypaste",
                    is_onoff=True)
        cls.add_arg("mouse_mode", "mouse_mode")
        cls.add_arg("filetransfer_enable", "filetransfer_enable",
                    is_onoff=True)
        cls.add_arg("gl", "gl", is_onoff=True)
        cls.add_arg("rendernode", "rendernode")


########################
# --controller parsing #
########################

class ParserController(VirtCLIParser):
    cli_arg_name = "controller"
    guest_propname = "devices.controller"
    remove_first = "type"

    def set_server_cb(self, inst, val, virtarg):
        inst.address.set_addrstr(val)

    def _parse(self, inst):
        if self.optstr == "usb2":
            return DeviceController.get_usb2_controllers(inst.conn)
        elif self.optstr == "usb3":
            return DeviceController.get_usb3_controller(inst.conn, self.guest)
        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type")
        cls.add_arg("model", "model")
        cls.add_arg("index", "index")
        cls.add_arg("master", "master_startport")
        cls.add_arg("driver_queues", "driver_queues")
        cls.add_arg("maxGrantFrames", "maxGrantFrames")

        cls.add_arg("address", None, lookup_cb=None, cb=cls.set_server_cb)


###################
# --input parsing #
###################

class ParserInput(VirtCLIParser):
    cli_arg_name = "input"
    guest_propname = "devices.input"
    remove_first = "type"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type", ignore_default=True)
        cls.add_arg("bus", "bus", ignore_default=True)


#######################
# --smartcard parsing #
#######################

class ParserSmartcard(VirtCLIParser):
    cli_arg_name = "smartcard"
    guest_propname = "devices.smartcard"
    remove_first = "mode"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("mode", "mode", ignore_default=True)
        cls.add_arg("type", "type", ignore_default=True)


######################
# --redirdev parsing #
######################

class ParserRedir(VirtCLIParser):
    cli_arg_name = "redirdev"
    guest_propname = "devices.redirdev"
    remove_first = "bus"
    stub_none = False

    def set_server_cb(self, inst, val, virtarg):
        inst.parse_friendly_server(val)

    def _parse(self, inst):
        if self.optstr == "none":
            self.guest.skip_default_usbredir = True
            return
        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("bus", "bus", ignore_default=True)
        cls.add_arg("type", "type", ignore_default=True)
        _add_device_boot_order_arg(cls)

        cls.add_arg("server", None, lookup_cb=None, cb=cls.set_server_cb)


#################
# --tpm parsing #
#################

class ParserTPM(VirtCLIParser):
    cli_arg_name = "tpm"
    guest_propname = "devices.tpm"
    remove_first = "type"

    def _parse(self, inst):
        if (self.optdict.get("type", "").startswith("/")):
            self.optdict["path"] = self.optdict.pop("type")
        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type")
        cls.add_arg("model", "model")
        cls.add_arg("version", "version")
        cls.add_arg("path", "device_path")


#################
# --rng parsing #
#################

class ParserRNG(VirtCLIParser):
    cli_arg_name = "rng"
    guest_propname = "devices.rng"
    remove_first = "type"
    stub_none = False

    def set_hosts_cb(self, inst, val, virtarg):
        namemap = {}
        inst.backend_type = inst.cli_backend_type

        if inst.cli_backend_mode == "connect":
            namemap["backend_host"] = "connect_host"
            namemap["backend_service"] = "connect_service"

        if inst.cli_backend_mode == "bind":
            namemap["backend_host"] = "bind_host"
            namemap["backend_service"] = "bind_service"

            if inst.cli_backend_type == "udp":
                namemap["backend_connect_host"] = "connect_host"
                namemap["backend_connect_service"] = "connect_service"

        if virtarg.cliname in namemap:
            util.set_prop_path(inst, namemap[virtarg.cliname], val)

    def set_backend_cb(self, inst, val, virtarg):
        if virtarg.cliname == "backend_mode":
            inst.cli_backend_mode = val
        elif virtarg.cliname == "backend_type":
            inst.cli_backend_type = val

    def _parse(self, inst):
        if self.optstr == "none":
            self.guest.skip_default_rng = True
            return

        inst.cli_backend_mode = "connect"
        inst.cli_backend_type = "udp"

        if self.optdict.get("type", "").startswith("/"):
            # Allow --rng /dev/random
            self.optdict["device"] = self.optdict.pop("type")
            self.optdict["type"] = "random"

        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type")

        cls.add_arg("backend_mode", None, lookup_cb=None,
                cb=cls.set_backend_cb)
        cls.add_arg("backend_type", None, lookup_cb=None,
                cb=cls.set_backend_cb)

        cls.add_arg("backend_host", None, lookup_cb=None,
                cb=cls.set_hosts_cb)
        cls.add_arg("backend_service", None, lookup_cb=None,
                cb=cls.set_hosts_cb)
        cls.add_arg("backend_connect_host", None, lookup_cb=None,
                cb=cls.set_hosts_cb)
        cls.add_arg("backend_connect_service", None, lookup_cb=None,
                cb=cls.set_hosts_cb)

        cls.add_arg("device", "device")
        cls.add_arg("model", "model")
        cls.add_arg("rate_bytes", "rate_bytes")
        cls.add_arg("rate_period", "rate_period")


######################
# --watchdog parsing #
######################

class ParserWatchdog(VirtCLIParser):
    cli_arg_name = "watchdog"
    guest_propname = "devices.watchdog"
    remove_first = "model"

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("model", "model", ignore_default=True)
        cls.add_arg("action", "action", ignore_default=True)


####################
# --memdev parsing #
####################

class ParserMemdev(VirtCLIParser):
    cli_arg_name = "memdev"
    guest_propname = "devices.memory"
    remove_first = "model"

    def set_target_size(self, inst, val, virtarg):
        util.set_prop_path(inst, virtarg.propname, int(val) * 1024)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("model", "model")
        cls.add_arg("access", "access")
        cls.add_arg("target.size", "target.size", cb=cls.set_target_size,
                aliases=["target_size"])
        cls.add_arg("target.node", "target.node",
                aliases=["target_node"])
        cls.add_arg("target.label_size", "target.label_size",
                cb=cls.set_target_size,
                aliases=["target_label_size"])
        cls.add_arg("source.pagesize", "source.pagesize",
                aliases=["source_pagesize"])
        cls.add_arg("source.path", "source.path",
                aliases=["source_path"])
        cls.add_arg("source.nodemask", "source.nodemask", can_comma=True,
                aliases=["source_nodemask"])


########################
# --memballoon parsing #
########################

class ParserMemballoon(VirtCLIParser):
    cli_arg_name = "memballoon"
    guest_propname = "devices.memballoon"
    remove_first = "model"
    stub_none = False

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("model", "model")


###################
# --panic parsing #
###################

class ParserPanic(VirtCLIParser):
    cli_arg_name = "panic"
    guest_propname = "devices.panic"
    remove_first = "model"
    compat_mode = False

    def set_model_cb(self, inst, val, virtarg):
        if self.compat_mode and val.startswith("0x"):
            inst.model = DevicePanic.MODEL_ISA
            inst.iobase = val
        else:
            inst.model = val

    def _parse(self, inst):
        if (len(self.optstr.split(",")) == 1 and
                not self.optstr.startswith("model=")):
            self.compat_mode = True
        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("model", "model", cb=cls.set_model_cb,
                    ignore_default=True)
        cls.add_arg("iobase", "iobase")


###################
# --vsock parsing #
###################

class ParserVsock(VirtCLIParser):
    cli_arg_name = "vsock"
    guest_propname = "devices.vsock"
    remove_first = "model"
    stub_none = False

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("model", "model")
        cls.add_arg("auto_cid", "auto_cid")
        cls.add_arg("cid", "cid")


######################################################
# --serial, --parallel, --channel, --console parsing #
######################################################

class _ParserChar(VirtCLIParser):
    remove_first = "char_type"
    stub_none = False

    def set_host_cb(self, inst, val, virtarg):
        if ("bind_host" not in self.optdict and
            self.optdict.get("mode", None) == "bind"):
            inst.set_friendly_bind(val)
        else:
            inst.set_friendly_source(val)

    def set_bind_cb(self, inst, val, virtarg):
        inst.set_friendly_bind(val)

    def set_target_cb(self, inst, val, virtarg):
        inst.set_friendly_target(val)

    def _parse(self, inst):
        if self.optstr == "none" and inst.DEVICE_TYPE == "console":
            self.guest.skip_default_console = True
            return
        if self.optstr == "none" and inst.DEVICE_TYPE == "channel":
            self.guest.skip_default_channel = True
            return

        return super()._parse(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        # _virtargs already populated via subclass creation, so
        # don't double register options
        if cls._virtargs:
            return

        VirtCLIParser._init_class(**kwargs)
        cls.add_arg("char_type", "type")
        cls.add_arg("path", "source_path")
        cls.add_arg("protocol",   "protocol")
        cls.add_arg("target_type", "target_type")
        cls.add_arg("name", "target_name")
        cls.add_arg("host", None, lookup_cb=None,
                cb=cls.set_host_cb)
        cls.add_arg("bind_host", None, lookup_cb=None,
                cb=cls.set_bind_cb)
        cls.add_arg("target_address", None, lookup_cb=None,
                cb=cls.set_target_cb)
        cls.add_arg("mode", "source_mode")
        cls.add_arg("source.master", "source_master")
        cls.add_arg("source.slave", "source_slave")
        cls.add_arg("log.file", "log_file")
        cls.add_arg("log.append", "log_append", is_onoff=True)


class ParserSerial(_ParserChar):
    cli_arg_name = "serial"
    guest_propname = "devices.serial"


class ParserParallel(_ParserChar):
    cli_arg_name = "parallel"
    guest_propname = "devices.parallel"


class ParserChannel(_ParserChar):
    cli_arg_name = "channel"
    guest_propname = "devices.channel"


class ParserConsole(_ParserChar):
    cli_arg_name = "console"
    guest_propname = "devices.console"


########################
# --filesystem parsing #
########################

class ParserFilesystem(VirtCLIParser):
    cli_arg_name = "filesystem"
    guest_propname = "devices.filesystem"
    remove_first = ["source", "target"]

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type")
        cls.add_arg("accessmode", "accessmode", aliases=["mode"])
        cls.add_arg("source", "source")
        cls.add_arg("target", "target")


###################
# --video parsing #
###################

class ParserVideo(VirtCLIParser):
    cli_arg_name = "video"
    guest_propname = "devices.video"
    remove_first = "model"

    def _parse(self, inst):
        ret = super()._parse(inst)

        if inst.conn.is_qemu() and inst.accel3d:
            if inst.model != "virtio":
                logging.warning("video model=%s does not support accel3d",
                    inst.model)
            elif not inst.conn.check_support(
                    inst.conn.SUPPORT_CONN_VIDEO_VIRTIO_ACCEL3D):
                logging.warning("qemu/libvirt version may not support "
                             "virtio accel3d")

        return ret

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("model", "model", ignore_default=True)
        cls.add_arg("accel3d", "accel3d", is_onoff=True)
        cls.add_arg("heads", "heads")
        cls.add_arg("ram", "ram")
        cls.add_arg("vram", "vram")
        cls.add_arg("vram64", "vram64")
        cls.add_arg("vgamem", "vgamem")


###################
# --sound parsing #
###################

class ParserSound(VirtCLIParser):
    cli_arg_name = "sound"
    guest_propname = "devices.sound"
    remove_first = "model"
    stub_none = False

    def _parse(self, inst):
        if self.optstr == "none":
            self.guest.skip_default_sound = True
            return
        return super()._parse(inst)

    def codec_find_inst_cb(self, *args, **kwargs):
        cliarg = "codec"  # codec[0-9]*
        list_propname = "codecs"
        cb = self._make_find_inst_cb(cliarg, list_propname)
        return cb(*args, **kwargs)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("model", "model", ignore_default=True)
        # Options for sound.codecs config
        cls.add_arg("codec[0-9]*.type", "type",
                            find_inst_cb=cls.codec_find_inst_cb)


#####################
# --hostdev parsing #
#####################

class ParserHostdev(VirtCLIParser):
    cli_arg_name = "hostdev"
    guest_propname = "devices.hostdev"
    remove_first = "name"

    def set_name_cb(self, inst, val, virtarg):
        if inst.type == "net":
            inst.mode = "capabilities"
            inst.net_interface = val
        elif inst.type == "misc":
            inst.mode = "capabilities"
            inst.misc_char = val
        elif inst.type == "storage":
            inst.mode = "capabilities"
            inst.storage_block = val
        else:
            val = NodeDevice.lookupNodedevFromString(inst.conn, val)
            inst.set_from_nodedev(val)

    def name_lookup_cb(self, inst, val, virtarg):
        nodedev = NodeDevice.lookupNodedevFromString(inst.conn, val)
        return nodedev.compare_to_hostdev(inst)

    @classmethod
    def _init_class(cls, **kwargs):
        VirtCLIParser._init_class(**kwargs)
        _add_device_address_args(cls)
        cls.add_arg("type", "type")
        cls.add_arg("name", None,
                    cb=cls.set_name_cb,
                    lookup_cb=cls.name_lookup_cb)
        cls.add_arg("driver_name", "driver_name")
        _add_device_boot_order_arg(cls)
        cls.add_arg("rom_bar", "rom_bar", is_onoff=True)


###########################
# Public virt parser APIs #
###########################

def parse_option_strings(options, guest, instlist, update=False):
    """
    Iterate over VIRT_PARSERS, and launch the associated parser
    function for every value that was filled in on 'options', which
    came from argparse/the command line.

    @update: If we are updating an existing guest, like from virt-xml
    """
    instlist = util.listify(instlist)
    if not instlist:
        instlist = [None]

    ret = []
    for parserclass in VIRT_PARSERS:
        optlist = util.listify(getattr(options, parserclass.cli_arg_name))
        if not optlist:
            continue

        for inst in instlist:
            if inst and optlist:
                # If an object is passed in, we are updating it in place, and
                # only use the last command line occurrence, eg. from virt-xml
                optlist = [optlist[-1]]

            for optstr in optlist:
                parserobj = parserclass(optstr, guest=guest)
                parseret = parserobj.parse(inst, validate=not update)
                ret += util.listify(parseret)

    return ret


def check_option_introspection(options):
    """
    Check if the user requested option introspection with ex: '--disk=?'
    """
    ret = False
    for parserclass in _get_completer_parsers():
        if not hasattr(options, parserclass.cli_arg_name):
            continue
        optlist = util.listify(getattr(options, parserclass.cli_arg_name))
        if not optlist:
            continue

        for optstr in optlist:
            if optstr == "?" or optstr == "help":
                parserclass.print_introspection()
                ret = True

    return ret
