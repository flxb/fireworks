"""
Support module for job packing.
This module contains function to prepare and launch the process.
Also, there is a DataServer class which provides share objects
Between processes.
"""
from multiprocessing import Process
import multiprocessing
from multiprocessing.managers import DictProxy
import os
import threading
import time
from fireworks.core.fw_config import FWConfig
from fireworks.features.jobpack_config import JPConfig, DataServer
from fireworks.core.launchpad import LaunchPad
from fireworks.core.rocket_launcher import rapidfire


__author__ = 'Xiaohui Qu, Anubhav Jain'
__copyright__ = 'Copyright 2013, The Material Project & The Electrolyte Genome Project'
__version__ = '0.1'
__maintainer__ = 'Xiaohui Qu'
__email__ = 'xqu@lbl.gov'
__date__ = 'Aug 19, 2013'



def create_launchpad(launchpad_file):
    '''
    Function to create the server side LaunchPad instance.
    This function will be called only once, only by the
    Manager server process.

    :param launchpad_file: (str) path to launchpad file
    :param strm_lvl: (str) level at which to output logs to stdout
    :return: (LaunchPad) object
    '''
    if launchpad_file:
        launchpad = LaunchPad.from_file(launchpad_file)
    else:
        launchpad = LaunchPad.auto_load()
    return launchpad


def manager_initializer():
    '''
    The intialization function for Manager server process.
    :return:
    '''
    jp_conf = JPConfig()
    jp_conf.MULTIPROCESSING = None # don't confuse the server process


def run_manager_server(launchpad_file, password):
    '''
    Start the Manager server process. The shared LaunchPad object proxy will
    be available after calling this function. Nothing to do with process
    management.

    :param launchpad_file: (str) path to launchpad file
    :param strm_lvl: (str) level at which to output logs
    :param port: (int) Listening port number
    :param password: (str) security password to access the server
    :return: (DataServer) object
    '''
    lp = create_launchpad(launchpad_file)
    DataServer.register('LaunchPad', callable=lambda: lp)
    running_ids = {}
    DataServer.register('Running_IDs', callable=lambda: running_ids, proxytype=DictProxy)
    m = DataServer(address=('127.0.0.1', 0), authkey=password)  # randomly pick a port
    m.start(initializer=manager_initializer)
    return m


def ping_launch_jp(port, password, stop_event):
    '''
    The process version of ping_launch

    :param port: (int) Listening port number of the shared object manage
    :param password: (str) security password to access the server
    :return:
    '''

    m = DataServer(address=('127.0.0.1', port), authkey=password)
    m.connect()
    lp = m.LaunchPad()
    while not stop_event.is_set():
        for pid, lid in m.Running_IDs().items():
            if lid:
                try:
                    os.kill(pid, 0)  # throws OSError if the process is dead
                    lp.ping_launch(lid)
                except OSError:
                    pass  # means this process is dead!

        stop_event.wait(FWConfig().PING_TIME_SECS)


def rapidfire_process(fworker, nlaunches, sleep, loglvl, port, password, node_list, sub_nproc, lock):
    '''
    Starting point of a sub job launching process.

    :param fworker: (FWorker) object
    :param nlaunches: (int) 0 means 'until completion', -1 or "infinite" means to loop forever
    :param sleep: (int) secs to sleep between rapidfire loop iterations
    :param loglvl: (str) level at which to output logs to stdout
    :param port: (int) Listening port number of the shared object manage
    :param password: (str) security password to access the server
    :param node_list: (list of str) computer node list
    :param sub_nproc: (int) number of processors of the sub job
    :param lock: (multiprocessing.Lock) Mutex
    :return:
    '''
    jp_conf = JPConfig()
    jp_conf.MULTIPROCESSING = True
    jp_conf.PACKING_MANAGER_PORT = port
    jp_conf.PACKING_MANAGER_PASSWORD = password
    jp_conf.NODE_LIST = node_list
    jp_conf.SUB_NPROCS = sub_nproc
    jp_conf.PROCESS_LOCK = lock
    m = DataServer(address=('127.0.0.1', port), authkey=password)
    m.connect()
    launchpad = m.LaunchPad()
    jp_conf.PACKING_MANAGER = m
    rapidfire(launchpad, fworker, None, nlaunches, -1, sleep, loglvl)


def launch_rapidfire_processes(fworker, nlaunches, sleep, loglvl, port, password, node_lists, sub_nproc_list):
    '''
    Create the sub job launching processes

    :param fworker: (FWorker) object
    :param nlaunches: nlaunches: (int) 0 means 'until completion', -1 or "infinite" means to loop forever
    :param sleep: (int) secs to sleep between rapidfire loop iterations
    :param loglvl: (str) level at which to output logs to stdout
    :param port: (int) Listening port number
    :param password: (str) security password to access the server
    :param node_lists: (list of str) computer node list
    :param sub_nproc_list: (list of int) list of the number of the process of sub jobs
    :return: (List of multiprocessing.Process) all the created processes
    '''
    lock = multiprocessing.Lock()
    processes = [Process(target=rapidfire_process, args=(fworker, nlaunches, sleep, loglvl, port, password, nl, sub_nproc, lock))
                 for nl, sub_nproc in zip(node_lists, sub_nproc_list)]
    for p in processes:
        p.start()
        time.sleep(0.15)
    return processes


def split_node_lists(num_rockets, total_node_list=None, ppn=24, serial_mode=False):
    '''
    Allocate node list of the large job to the sub jobs

    :param num_rockets: (int) number of sub jobs
    :param total_node_list: (list of str) the node list of the whole large job
    :param ppn: (int) number of procesors per node
    :return: (list of list) NODELISTs
    '''
    if serial_mode:
        if total_node_list:
            orig_node_list = sorted(list(set(total_node_list)))
            nnodes = len(orig_node_list)
            job_per_node = num_rockets/nnodes
            if job_per_node*nnodes != num_rockets:
                raise ValueError("can't allocate processes, {} can't be divided by {}".format(num_rockets, nnodes))
            sub_nproc_list = [1] * num_rockets
            node_lists = [[node] for node in orig_node_list * job_per_node]
        else:
            sub_nproc_list = [1] * num_rockets
            node_lists = [None] * num_rockets
    else:
        if total_node_list:
            orig_node_list = sorted(list(set(total_node_list)))
            nnodes = len(orig_node_list)
            sub_nnodes = nnodes/num_rockets
            if sub_nnodes*num_rockets != nnodes:
                raise ValueError("can't allocate nodes, {} can't be divided by {}".format(nnodes, num_rockets))
            sub_nproc_list = [sub_nnodes * ppn] * num_rockets
            node_lists = [orig_node_list[i:i+sub_nnodes] for i in range(0, nnodes, sub_nnodes)]
        else:
            sub_nproc_list = [ppn] * num_rockets
            node_lists = [None] * num_rockets
    return node_lists, sub_nproc_list

def launch_job_packing_processes(fworker, launchpad_file, loglvl, nlaunches,
                                 num_rockets, password, sleep_time,
                                 total_node_list=None, ppn=24, serial_mode=False):
    '''
    Launch the jobs in the job packing mode.
    :param fworker: (FWorker) object
    :param launchpad_file: (str) path to launchpad file
    :param loglvl: (str) level at which to output logs
    :param nlaunches: (int) 0 means 'until completion', -1 or "infinite" means to loop forever
    :param num_rockets: (int) number of sub jobs
    :param password: (str) security password to access the shared object server
    :param sleep_time: (int) secs to sleep between rapidfire loop iterations
    :return:
    '''
    node_lists, sub_nproc_list = split_node_lists(num_rockets, total_node_list, ppn, serial_mode)
    m = run_manager_server(launchpad_file, password)
    port = m.address[1]
    processes = launch_rapidfire_processes(fworker, nlaunches, sleep_time, loglvl,
                                           port, password, node_lists, sub_nproc_list)

    ping_stop = threading.Event()
    ping_thread = threading.Thread(target=ping_launch_jp, args=(port, password, ping_stop))
    ping_thread.start()

    for p in processes:
        p.join()
    ping_stop.set()
    m.shutdown()