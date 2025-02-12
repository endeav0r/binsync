# ----------------------------------------------------------------------------
# This file contains the BinSyncController class which acts as the the
# bridge between the plugin UI and direct calls to the binsync client found in
# the core of binsync. In the controller, you will find code used to make
# pushes and pulls of user changes.
#
# You will also notice that the BinSyncController runs two extra threads in
# it:
#   1. BinSync "git pulling" thread to constantly get changes from others
#   2. Command Routine to get hooked changes to IDA attributes
#
# The second point is more complicated because it acts as the queue of
# runnable actions that are queued from inside the hooks.py file.
# Essentially, every change that happens in IDA from the main user triggers
# a hook which will push an action to be preformed onto the command queue;
# Causing a "git push" on every change.
#
# ----------------------------------------------------------------------------

from functools import wraps
import re
import threading
import time
import datetime
import logging
from typing import Dict, List, Tuple
from collections import OrderedDict, defaultdict

from PyQt5.QtWidgets import QMessageBox

import idc
import idaapi
import idautils
import ida_struct
import ida_hexrays
import ida_funcs

import binsync
from binsync import Client, ConnectionWarnings
from binsync.data import StackVariable, StackOffsetType, Function, Struct, Comment
from . import compat

_l = logging.getLogger(name=__name__)

#
# Decorators
#


def init_checker(f):
    @wraps(f)
    def initcheck(self, *args, **kwargs):
        if not self.check_client():
            raise RuntimeError("Please connect to a repo first.")
        return f(self, *args, **kwargs)
    return initcheck


def make_state(f):
    """
    Build a writeable State instance and pass to `f` as the `state` kwarg if the `state` kwarg is None.
    Function `f` should have have at least two kwargs, `user` and `state`.
    """
    @wraps(f)
    def state_check(self, *args, **kwargs):
        state = kwargs.pop('state', None)
        user = kwargs.pop('user', None)
        if state is None:
            state = self.client.get_state(user=user)
            kwargs['state'] = state
            r = f(self, *args, **kwargs)
            state.save()
        else:
            kwargs['state'] = state
            r = f(self, *args, **kwargs)

        #try:
        #    if isinstance(args[0], int):
        #        self._update_function_name_if_none(args[0], user=user, state=state)
        #except Exception:
        #    print(f"[BinSync]: failed to auto set function name for {hex(args[0])}.")
        #    pass

        return r

    return state_check


def make_ro_state(f):
    """
    Build a read-only State instance and pass to `f` as the `state` kwarg if the `state` kwarg is None.
    Function `f` should have have at least two kwargs, `user` and `state`.
    """
    @wraps(f)
    def state_check(self, *args, **kwargs):
        state = kwargs.pop('state', None)
        user = kwargs.pop('user', None)
        if state is None:
            state = self.client.get_state(user=user)
        kwargs['state'] = state
        kwargs['user'] = user
        return f(self, *args, **kwargs)
    return state_check


#
#   Wrapper Classes
#

class UpdateTask:
    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def __eq__(self, other):
        return (
            isinstance(other, UpdateTask)
            and other.func == other.func
            and other.kwargs == other.kwargs
        )

    def __hash__(self):
        expanded_kwargs = list()
        for k, v in self.kwargs.items():
            expanded_kwargs.append(f"{k}={v}")
        return hash((self.func, *self.args, *expanded_kwargs))

    def dump(self):
        return self.func, self.args, self.kwargs


class UpdateTaskState:
    def __init__(self):
        self.update_tasks: Dict[UpdateTask, bool] = OrderedDict()
        self.update_tasks_lock = threading.Lock()

    def toggle_auto_sync_task(self, update_task):
        with self.update_tasks_lock:
            # delete the task if it is an auto_sync task already
            if update_task in list(self.update_tasks.keys()) and self.update_tasks[update_task]:
                del self.update_tasks[update_task]
                return False

            # set/make the task if its not auto_sync already
            self.update_tasks[update_task] = True
            return True

    def add_update_task(self, update_task):
        with self.update_tasks_lock:
            self.update_tasks[update_task] = False

    def do_needed_updates(self):
        with self.update_tasks_lock:
            # run each task in the update task queue
            for update_task in list(self.update_tasks.keys()):
                f, args, kwargs = update_task.dump()
                auto_sync = self.update_tasks[update_task]

                # doing task
                try:
                    f(*args, **kwargs)
                except Exception:
                    print(f"[BinSync]: failed to execute cache of {f} with {args}")

                # remove the task if its not an auto_sync task
                if not auto_sync:
                    del self.update_tasks[update_task]


class SyncControlStatus:
    CONNECTED = 0
    CONNECTED_NO_REMOTE = 1
    DISCONNECTED = 2


#
#   Controller
#

class BinsyncController:
    def __init__(self):
        self.client = None  # type: binsync.Client

        # === UI update things ===
        self.info_panel = None
        self._last_reload = time.time()

        # === locks needed for hooking threads ===
        # command queue locks
        self.queue_lock = threading.Lock()
        self.cmd_queue = OrderedDict()
        # api locks
        self.api_lock = threading.Lock()
        self.api_count = 0

        # start the pull routine
        self.pull_thread = threading.Thread(target=self.pull_routine)
        self.pull_thread.setDaemon(True)
        self.pull_thread.start()

        # update state for only updating when needed
        self.update_states = defaultdict(UpdateTaskState)

    #
    #   Multithreaded locks and setters
    #

    def inc_api_count(self):
        with self.api_lock:
            self.api_count += 1

    def make_controller_cmd(self, cmd_func, *args, **kwargs):
        with self.queue_lock:
            if cmd_func == self.push_struct:
                self.cmd_queue[args[0].name] = (cmd_func, args, kwargs)
            else:
                self.cmd_queue[time.time()] = (cmd_func, args, kwargs)

    def eval_cmd_queue(self):
        self.queue_lock.acquire()
        if len(self.cmd_queue) > 0:
            # pop the first command from the queue
            cmd = self.cmd_queue.popitem(last=False)[1]
            self.queue_lock.release()

            # parse the command
            func = cmd[0]
            f_args = cmd[1]
            f_kargs = cmd[2]

            # call it!
            func(*f_args, **f_kargs)
            return 0
        self.queue_lock.release()

    def pull_routine(self):
        while True:
            # pull the repo every 10 seconds
            if self.check_client() and self.client.has_remote \
                    and (
                    self.client._last_pull_attempt_at is None
                    or (datetime.datetime.now() - self.client._last_pull_attempt_at).seconds > 10
                         ):
                # Pull new items
                self.client.pull()

            if self.check_client():
                # run an operation every second
                self.eval_cmd_queue()

                # reload info panel every 10 seconds
                if self.info_panel is not None and time.time() - self._last_reload > 10:
                    try:
                        self._last_reload = time.time()
                        self.info_panel.reload()
                    except RuntimeError:
                        # the panel has been closed
                        self.info_panel = None

            # Snooze
            time.sleep(1)

    #
    #   State Interaction Functions
    #

    def connect(self, user, path, init_repo=False, remote_url=None):
        binary_md5 = idc.retrieve_input_file_md5().hex()
        self.client = Client(user, path, binary_md5,
                             init_repo=init_repo,
                             remote_url=remote_url,
                             )
        BinsyncController._parse_and_display_connection_warnings(self.client.connection_warnings)
        print(f"[BinSync]: Client has connected to sync repo with user: {user}.")

    def check_client(self, message_box=False):
        if self.client is None:
            if message_box:
                QMessageBox.critical(
                    None,
                    "BinSync: Error",
                    "BinSync client does not exist.\n"
                    "You haven't connected to a binsync repo. Please connect to a binsync repo first.",
                    QMessageBox.Ok,
                )
            return False
        return True

    def state_ctx(self, user=None, version=None, locked=False):
        return self.client.state_ctx(user=user, version=version, locked=locked)

    def status(self):
        if self.check_client():
            if self.client.has_remote:
                return SyncControlStatus.CONNECTED
            return SyncControlStatus.CONNECTED_NO_REMOTE
        return SyncControlStatus.DISCONNECTED

    def status_string(self):
        stat = self.status()
        if stat == SyncControlStatus.CONNECTED:
            return f"Connected to a sync repo: {self.client.master_user}"
        elif stat == SyncControlStatus.CONNECTED_NO_REMOTE:
            return f"Connected to a sync repo (no remote): {self.client.master_user}"
        else:
            return "Not connected to a sync repo"

    @init_checker
    def users(self):
        return self.client.users()

    #
    #   IDA DataBase Fillers
    #

    @init_checker
    @make_ro_state
    def fill_structs(self, user=None, state=None):
        """
        Grab all the structs from a specified user, then fill them locally

        @param user:
        @param state:
        @return:
        """
        # sanity check, the desired user has some structs to sync
        pulled_structs: List[Struct] = self.pull_structs(user=user, state=state)
        if len(pulled_structs) <= 0:
            print(f"[BinSync]: User {user} has no structs to sync!")
            return 0

        # convert each binsync struct into an ida struct and set it in the GUI
        for struct in pulled_structs:
            compat.set_ida_struct(struct, self)

        # set the type of each member in the structs
        all_typed_success = True
        for struct in pulled_structs:
            all_typed_success &= compat.set_ida_struct_member_types(struct, self)

        return all_typed_success

    @init_checker
    @make_ro_state
    def fill_function(self, func_addr, user=None, state=None):
        """
        Grab all relevant information from the specified user and fill the @ida_func.
        """

        # === sanity and cache checks === #
        # check that this function exists in IDA
        ida_func = ida_funcs.get_func(func_addr)
        if ida_func is None:
            print(f"[BinSync]: IDA Error on sync for \'{user}\' on function {hex(func_addr)}.")
            return -1

        # preform a diff check to see if we need to do a change
        master_state = self.client.get_state(user=self.client.master_user)
        no_change = master_state.compare_function(func_addr, state)
        if no_change:
            print(f"[BinSync]: No change on sync for \'{user}\' on function {hex(func_addr)}.")
            return 0

        # check if the function exists in the pulled state
        _func = self.pull_function(ida_func, user=user, state=state)
        if _func is None:
            return -1

        # === function name === #
        if _func.name and _func.name != "" and compat.get_func_name(ida_func.start_ea) != _func.name:
            self.inc_api_count()
            compat.set_ida_func_name(ida_func.start_ea, _func.name)

        # === comments === #
        # set disassembly and decompiled comments
        sync_cmts = self.pull_comments(ida_func.start_ea, user=user, state=state)
        for addr, cmt in sync_cmts.items():
            self.inc_api_count()
            res = compat.set_ida_comment(addr, cmt.comment, decompiled=cmt.decompiled)
            if not res:
                # XXX: this can be dangerous:
                # if the above comment fails and the api_count never gets decreased after
                # getting increased, we can be stalled for a long time.
                print(f"[BinSync]: Failed to sync comment at <{hex(addr)}>: \'{cmt.comment}\'")

        # === stack variables === #
        # sanity check that this function has a stack frame
        frame = idaapi.get_frame(ida_func.start_ea)
        if frame is None or frame.memqty <= 0:
            _l.debug("Function %#x does not have an associated function frame. Skip variable name sync-up.",
                     ida_func.start_ea)
            return -1

        # collect and covert the info of each stack variable
        existing_stack_vars = {}
        for offset, ida_var in compat.get_func_stack_var_info(ida_func.start_ea).items():
            existing_stack_vars[compat.ida_to_angr_stack_offset(ida_func.start_ea, offset)] = ida_var

        stack_vars_to_set = {}
        ida_code_view = ida_hexrays.open_pseudocode(ida_func.start_ea, 0)
        # only try to set stack vars that actually exist
        for offset, stack_var in self.pull_stack_variables(ida_func, user=user, state=state).items():
            if offset in existing_stack_vars:
                # change the variable's name
                if stack_var.name != existing_stack_vars[offset].name:
                    self.inc_api_count()
                    ida_struct.set_member_name(frame, existing_stack_vars[offset].offset, stack_var.name)

                # check if the variables type should be changed
                if ida_code_view and stack_var.type != existing_stack_vars[offset].type_str:
                    # validate the type is convertible
                    ida_type = compat.convert_type_str_to_ida_type(stack_var.type)
                    if ida_type is None:
                        # its possible the type is just a custom type from the same user
                        # TODO: make it possible to sync a single struct
                        if self._typestr_in_state_structs(stack_var.type, user=user, state=state):
                            self.fill_structs(user=user, state=state)

                        ida_type = compat.convert_type_str_to_ida_type(stack_var.type)
                        # it really is just a bad type
                        if ida_type is None:
                            print(f"[BinSync]: Failed to parse stack variable stored type at offset"
                                  f" {hex(existing_stack_vars[offset].offset)} with type {stack_var.type}"
                                  f" on function {hex(ida_func.start_ea)}.")
                            continue

                    # queue up the change!
                    stack_vars_to_set[existing_stack_vars[offset].offset] = ida_type

            # change the type of all vars that need to be changed
            # NOTE: api_count is incremented inside the function
            compat.set_stack_vars_types(stack_vars_to_set, ida_code_view, self)

        # ===== update the pseudocode ==== #
        compat.refresh_pseudocode_view(_func.addr)
        print(f"[Binsync]: New data synced for \'{user}\' on function {hex(ida_func.start_ea)}.")

    #
    #   Pullers
    #

    @init_checker
    def sync_all(self, user=None, state=None):
        # copy the actual state from the other user
        self.client.sync_states(user=user)
        new_state = self.client.get_state(user=self.client.master_user)
        func_addrs = new_state.functions.keys()
        print("[BinSync]: Target Addrs for sync being cached:", [hex(x) for x in func_addrs])

        # set the new stuff in the UI
        for func_addr in func_addrs:
            update_task = UpdateTask(
                self.fill_function,
                func_addr, user=self.client.master_user
            )
            self.update_states[func_addr].add_update_task(update_task)

    @init_checker
    @make_ro_state
    def pull_function(self, ida_func, user=None, state=None):
        """
        Pull a function downwards.

        :param bv:
        :param bn_func:
        :param user:
        :return:
        """

        # pull function
        try:
            if hasattr(ida_func, "start_ea"):
                func: Function = state.get_function(ida_func.start_ea)
                return func
            else:
                print("[BinSync]: IDA Function does not exist")
                return None
        except KeyError:
            return None

    @init_checker
    @make_ro_state
    def pull_stack_variables(self, ida_func, user=None, state=None):
        try:
            return dict(state.get_stack_variables(ida_func.start_ea))
        except KeyError:
            return { }

    @init_checker
    @make_ro_state
    def pull_stack_variable(self, ida_func, offset, user=None, state=None):
        return state.get_stack_variable(ida_func.start_ea, offset)

    @init_checker
    @make_ro_state
    def pull_comments(self, func_addr, user=None, state=None) -> Dict[int, List['Comment']]:
        try:
            return state.get_comments(func_addr)
        except KeyError:
            return {}

    @init_checker
    @make_ro_state
    def pull_comment(self, func_addr, addr, user=None, state=None):
        try:
            return state.get_comment(func_addr, addr)
        except KeyError:
            return None

    @init_checker
    @make_ro_state
    def pull_structs(self, user=None, state=None):
        """
        Pull structs downwards.

        @param user:
        @param state:
        @return:
        """
        return state.get_structs()

    @init_checker
    @make_state
    def remove_all_comments(self, ida_func, user=None, state=None):
        for start_ea, end_ea in idautils.Chunks(ida_func):
            for ins_addr in idautils.Heads(start_ea, end_ea):
                if ins_addr in state.comments:
                    state.remove_comment(ins_addr)

    #
    #   Pushers
    #

    @init_checker
    @make_state
    def push_comment(self, func_addr, addr, comment, decompiled=False,
                     user=None, state: "binsync.State" = None, api_set=False):
        sync_cmt = binsync.data.Comment(func_addr, addr, comment, decompiled=decompiled)
        state.set_comment(sync_cmt, set_last_change=not api_set)

    def push_comments(self, func_addr, cmt_dict: Dict[int, str], decompiled=False,
                      user=None, state: "binsync.State" = None, api_set=False):
        for addr in cmt_dict:
            self.push_comment(func_addr, addr, cmt_dict[addr], decompiled=decompiled,
                              user=user, state=state, api_set=api_set)
        
    '''
    # TODO: Just pass along the offset. Why the whole patch ??
    @init_checker
    @make_state
    def push_patch(self, patch, user=None, state=None, api_set=False):
        # Update last pushed values
        push_time = int(time.time())
        last_push_func = compat.ida_func_addr(patch.offset)
        func_name = compat.get_func_name(last_push_func)

        state.set_patch(patch.offset, patch)
        self.client.set_last_push(last_push_func, push_time, func_name)
    '''

    @init_checker
    @make_state
    def push_function_name(self, attr_addr, new_name,
                           user=None, state: "binsync.State" = None, api_set=False):
        # setup the new function for binsync
        func = binsync.data.Function(attr_addr)
        func.name = new_name
        state.set_function(func, set_last_change=not api_set)

    @init_checker
    @make_state
    def push_stack_variable(self, attr_addr, stack_offset, name, type_str, size,
                            user=None, state: "binsync.State" = None, api_set=False):
        # convert longs to ints
        stack_offset = int(stack_offset)
        func_addr = int(attr_addr)
        size = int(size)

        v = StackVariable(stack_offset,
                          StackOffsetType.IDA,
                          name,
                          type_str,
                          size,
                          func_addr)
        state.set_stack_variable(v, stack_offset, func_addr, set_last_change=not api_set)

    @init_checker
    @make_state
    def push_struct(self, struct, old_name,
                    user=None, state=None, api_set=False):
        old_name = None if old_name == "" else old_name
        state.set_struct(struct, old_name, set_last_change=not api_set)

    #
    # Utils
    #

    def _update_function_name_if_none(self, func_addr, state=None, user=None):
        curr_name = compat.get_func_name(func_addr)
        if state.functions[func_addr].name is None or state.functions[func_addr].name == "":
            state.functions[func_addr].name = curr_name
            state.save()

    @init_checker
    def _typestr_in_state_structs(self, type_str, user=None, state=None):
        binsync_structs = state.get_structs()
        for struct in binsync_structs:
            if struct.name in type_str:
                return True

        return False


    @staticmethod
    def _parse_and_display_connection_warnings(warnings):
        warning_text = ""

        for warning in warnings:
            if warning == ConnectionWarnings.HASH_MISMATCH:
                warning_text += "Warning: the hash stored for this BinSync project does not match"
                warning_text += " the hash of the binary you are attempting to analyze. It's possible"
                warning_text += " you are working on a different binary.\n"

        if len(warning_text) > 0:
            QMessageBox.warning(
                None,
                "BinSync: Connection Warnings",
                warning_text,
                QMessageBox.Ok,
            )

    @staticmethod
    def get_default_type_str(flag):
        if idc.is_byte(flag):
            return "unsigned char"
        elif idc.is_word(flag):
            return "unsigned short"
        elif idc.is_dword(flag):
            return "unsigned int"
        elif idc.is_qword(flag):
            return "unsigned long long"
        else:
            return "unknown"

    @staticmethod
    def friendly_datetime(time_before):
        # convert
        if isinstance(time_before, int):
            dt = datetime.datetime.fromtimestamp(time_before)
        elif isinstance(time_before, datetime.datetime):
            dt = time_before
        else:
            return ""

        now = datetime.datetime.now()
        if dt <= now:
            diff = now - dt
            ago = True
        else:
            diff = dt - now
            ago = False
        diff_days = diff.days
        diff_sec = diff.seconds

        if diff_days >= 1:
            s = "%d days" % diff_days
        elif diff_sec >= 60 * 60:
            s = "%d hours" % int(diff_sec / 60 / 60)
        elif diff_sec >= 60:
            s = "%d minutes" % int(diff_sec / 60)
        else:
            s = "%d seconds" % diff_sec

        s += " ago" if ago else " in the future"
        return s
