#!/usr/bin/env python3

"""

Config file format (example):

    [DEFAULT]
    local_bdsync_bin = /usr/local/bin/bdsync
    remote_bdsync_bin = /usr/local/bin/bdsync
    connection_command = ssh -p 2200 foo@target
    target_patch_dir = /tmp
    bdsync_args = --hash=sha1 --diffsize=resize

    [example-root]
    source_path = /dev/lvm-foo/example-root
    target_path = backup/example-root
    lvm_snapshot_enabled = yes
    lvm_snapshot_size = 5G
    lvm_snapshot_name = bdsync-snapshot

The above values do not represent defaults.
"""

import argparse
import configparser
import datetime
import logging
import os
import re
import shlex
import tempfile
import time

import plumbum


LVM_SIZE_REGEX = re.compile(r"^[0-9]+[bBsSkKmMgGtTpPeE]?$")


class BDSyncManagerError(Exception): pass
class TaskProcessingError(BDSyncManagerError): pass
class TaskSettingsError(BDSyncManagerError): pass


def load_settings(config):
    # load and validate settings
    settings = {}
    try:
        settings["local_bdsync_bin"] = config["local_bdsync_bin"]
        settings["remote_bdsync_bin"] = config["remote_bdsync_bin"]
        settings["bdsync_args"] = config.get("bdsync_args", "")
        settings["source_path"] = config["source_path"]
        settings["target_path"] = config["target_path"]
        settings["disabled"] = config.getboolean("disabled", False)
        settings["connection_command"] = config.get("connection_command", None)
        settings["target_patch_dir"] = config.get("target_patch_dir", None)
        lvm_snapshot_enabled = config.getboolean("lvm_snapshot_enabled", False)
        if lvm_snapshot_enabled:
            settings["lvm"] = {
                    "snapshot_size": config["lvm_snapshot_size"],
                    "snapshot_name": config.get("lvm_snapshot_name", "bdsync-snapshot"),
            }
    except configparser.NoOptionError as exc:
        raise TaskSettingsError("Missing a mandatory task option: %s" % str(exc))
    return settings


def validate_settings(settings):
    # validate input
    if not os.path.isfile(settings["local_bdsync_bin"]):
        raise TaskSettingsError("The local 'bdsync' binary was not found (%s)." % settings["local_bdsync_bin"])
    if not os.path.exists(settings["source_path"]):
        raise TaskSettingsError("The source device (source_path=%s) does not exist" % settings["source_path"])
    if "lvm" in settings:
        if not LVM_SIZE_REGEX.match(settings["lvm"]["snapshot_size"]):
            raise TaskSettingsError("Invalid LVM snapshot size (%s)" % settings["lvm"]["snapshot_size"])
        vg_name = plumbum.local["lvs"]("--noheadings", "--options", "vg_name", settings["source_path"]).strip()
        if not vg_name:
            raise TaskSettingsError("Failed to discover the name of the Volume Group of '{source}' via 'lvs'"
                                      .format(source=settings["source_path"]))
        settings["lvm"]["vg_name"] = vg_name
    if not settings["connection_command"]:
        # local transfer
        if not os.path.exists(os.path.dirname(settings["target_path"])):
            raise TaskSettingsError("The directory of the local target (target_path=%s) does not exist" % \
                    settings["target_path"])
        if not os.path.isdir(settings["target_patch_dir"]):
            raise TaskSettingsError("The patch directory of the local target (target_patch_dir=%s) does not exist" % \
                    settings["target_patch_dir"])


def get_remote_tempfile(connection_command, target, directory):
    cmd_args = shlex.split(connection_command)
    cmd_args.append("mktemp --tmpdir=%s %s-XXXX.bdsync" % (shlex.quote(directory), shlex.quote(os.path.basename(target))))
    cmd_command = cmd_args.pop(0)
    output = plumbum.local[cmd_command](cmd_args)
    # remove linebreaks from result
    return output.rstrip("\n\r")


def sizeof_fmt(num, suffix='B'):
    # source: http://stackoverflow.com/a/1094933
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def run_bdsync(source, target, target_patch_dir, connection_command, local_bdsync, remote_bdsync, bdsync_args):
    log.info("Creating binary patch")
    if connection_command:
        # prepend the connection command
        remote_command = "%s %s --server" % (connection_command, shlex.quote(remote_bdsync))
        remote_patch_file = get_remote_tempfile(connection_command, target, target_patch_dir)
        log.debug("Using remote temporary patch file: %s" % str(remote_patch_file))
        output_command_args = shlex.split(connection_command)
        output_command_args.append("cat > %s" % shlex.quote(remote_patch_file))
        log.debug("Using remote patch transfer: %s" % str(output_command_args))
        output_command_command = output_command_args.pop(0)
        output_command = plumbum.local[output_command_command][tuple(output_command_args)]
        patch_size_args = shlex.split(connection_command)
        # "stat --format %s" returns the size of the file in bytes
        patch_size_args.append("stat --format %%s %s" % shlex.quote(remote_patch_file))
        patch_size_command = patch_size_args.pop(0)
        patch_size_func = plumbum.local[patch_size_command][tuple(patch_size_args)]
    else:
        remote_command = "%s --server" % shlex.quote(remote_bdsync)
        local_patch_file = tempfile.NamedTemporaryFile(dir=target_patch_dir, delete=False)
        patch_size_func = lambda: os.path.getsize(local_patch_file.name)
        output_command = None
    # run bdsync and handle the resulting states
    create_patch_args = []
    create_patch_args.append(local_bdsync)
    create_patch_args.extend(shlex.split(bdsync_args))
    create_patch_args.append(remote_command)
    create_patch_args.append(source)
    create_patch_args.append(target)
    create_patch_command = create_patch_args.pop(0)
    patch_source = plumbum.local[create_patch_command][tuple(create_patch_args)]
    patch_create_start_time = time.time()
    if connection_command:
        chain = patch_source | output_command
    else:
        chain = patch_source > local_patch_file
    log.debug("Starting local bdsync process: %s" % str(args))
    chain()
    patch_create_time = datetime.timedelta(seconds=(time.time() - patch_create_start_time))
    log.debug("bdsync successfully created and transferred a binary patch")
    log.info("Patch Create Time: %s" % patch_create_time)
    log.info("Patch Size: %s" % sizeof_fmt(int(patch_size_func())))
    patch_apply_start_time = time.time()
    # everything went fine - now the patch should be applied
    if connection_command:
        patch_source = None
        # remote command: "bdsync [bdsync_args] --patch < PATCH_FILE"
        remote_command_tokens = []
        remote_command_tokens.append(remote_bdsync)
        remote_command_tokens.extend(shlex.split(bdsync_args))
        remote_command_tokens.append("--patch")
        remote_command_combined = " ".join([shlex.quote(token) for token in remote_command_tokens])
        # the input file is added after an unquoted "<"
        remote_command_combined += " < %s" % shlex.quote(remote_patch_file)
        # local command: "ssh foo@bar 'REMOTE_COMMAND'"
        patch_call_args = shlex.split(connection_command)
        patch_call_args.append(remote_command_combined)
        patch_call_command = patch_call_args.pop(0)
        apply_patch = plumbum.local[patch_call_command][tuple(patch_call_args)]
    else:
        local_patch_file.seek(0)
        patch_call_args = shlex.split(bdsync_args) + ["--patch"]
        apply_patch = (plumbum.local[local_bdsync][tuple(patch_call_args)] < local_patch_file)
    log.debug("Applying the patch")
    log.debug("bdsync patching: %s" % str(apply_patch))
    apply_patch()
    patch_apply_time = datetime.timedelta(seconds=(time.time() - patch_apply_start_time))
    log.debug("Successfully applied the patch")
    log.info("Patch Apply Time: %s" % patch_apply_time)
    if connection_command:
        # remove remote patch file
        remove_args = shlex.split(connection_command)
        remove_args.append("rm %s" % shlex.quote(remote_patch_file))
        remove_command = remove_args.pop(0)
        log.debug("Removing the remote temporary patch file: %s" % str(remove_args))
        plumbum.local[remove_command](remove_args)
    else:
        os.unlink(local_patch_file.name)


def process_task(config):
    settings = load_settings(config)
    if settings["disabled"]:
        log.info("Skipping disabled task")
        return
    validate_settings(settings)
    # everything looks fine - we can start
    if "lvm" in settings:
        real_source = prepare_lvm_snapshot(settings["source_path"], settings["lvm"]["vg_name"],
                settings["lvm"]["snapshot_name"], settings["lvm"]["snapshot_size"])
    else:
        real_source = settings["source_path"]
    try:
        run_bdsync(real_source, settings["target_path"], settings["target_patch_dir"],
                settings["connection_command"], settings["local_bdsync_bin"], settings["remote_bdsync_bin"], settings["bdsync_args"])
    finally:
        if "lvm" in settings:
            cleanup_lvm_snapshot(settings["lvm"]["vg_name"], settings["lvm"]["snapshot_name"])


def prepare_lvm_snapshot(source_path, vg_name, snapshot_name, snapshot_size):
    log.info("Creating LVM snapshot: {vg_name}/{snapshot_name}".format(vg_name=vg_name, snapshot_name=snapshot_name))
    plumbum.local["lvcreate"]("--snapshot", "--name", snapshot_name, "--size", snapshot_size, source_path)
    return "/dev/{vg_name}/{snapshot_name}".format(vg_name=vg_name, snapshot_name=snapshot_name)


def cleanup_lvm_snapshot(vg_name, snapshot_name):
    log.info("Removing LVM snapshot: {vg_name}/{snapshot_name}".format(vg_name=vg_name, snapshot_name=snapshot_name))
    plumbum.local["lvremove"]("--force", "%s/%s" % (vg_name, snapshot_name))


def parse_arguments():
    parser = argparse.ArgumentParser(description="Manage one or more bdsync transfers.")
    parser.add_argument("--log-level", dest="log_level", default="warning",
            choices=("debug", "info", "warning", "error"), help="Output verbosity")
    parser.add_argument("--config", metavar="CONFIG_FILE", dest="config_file",
            default="/etc/bdsync-manager.conf", type=argparse.FileType('r'),
            help="Location of the config file")
    parser.add_argument("--task", metavar="TASK_NAME", dest="tasks", action="append")
    args = parser.parse_args()
    log_levels = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
    }
    log.setLevel(log_levels[args.log_level])
    return args


def _get_safe_string(text):
    return re.sub(r"\W", "_", text)


if __name__ == "__main__":
    log = logging.getLogger("bdsync-manager")
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter("[bdsync-manager] %(asctime)s - %(message)s"))
    log_handler.setLevel(logging.DEBUG)
    log.addHandler(log_handler)
    log.debug("Parsing arguments")
    args = parse_arguments()
    config = configparser.ConfigParser()
    log.debug("Reading config file: %s" % str(args.config_file.name))
    config.read_file(args.config_file)
    if args.tasks:
        tasks = []
        for task in args.tasks:
            if task in config.sections():
                tasks.append(task)
            else:
                log.warning("Skipping unknown task: %s" % _get_safe_string(task))
    else:
        tasks = config.sections()
    if not tasks:
        log.warning("There is nothing to be done (no tasks found in config file).")
    try:
        for task in tasks:
            log_handler.setFormatter(logging.Formatter("[Task '%s'] %%(levelname)s: %%(message)s" % str(task)))
            try:
                process_task(config[task])
            except TaskProcessingError as exc:
                log.error(str(exc))
    except KeyboardInterrupt:
        log.info("Cancelled task")
