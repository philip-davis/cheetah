"""
Generate performance report from a completed campaign.
This module parses all run directories in all sweep groups to aggregate
information.
Runs sosflow analysis to collect data.

All parameters specified in the spec file must be used as column headers in
an output csv file.
"""

import re
import os
from pathlib import Path
import json
import csv
from codar.cheetah import sos_flow_analysis
from codar.cheetah.helpers import get_immediate_subdirs


def __serialize_params_nested_dict(nested_run_params_dict):
    """
    codar.cheetah.run-params.json has the structure:
    {
        app1: {
            param1: value1
            param2: value2
        }
        app2: {
            param1: value1
            param2: value2
        }
    }

    Serialize this structure so that we have
    {app1__param1: value1, app1__param2:value2, and so on}.
    """
    serialized_dict = {}
    for key in nested_run_params_dict:
        for nested_key in nested_run_params_dict[key]:
            new_key = key + "__" + nested_key
            serialized_dict[new_key] = nested_run_params_dict[key][nested_key]

    return serialized_dict


def __parse_run_dir(run_dir, parsed_runs, unique_keys):
    """
    Parse run directory of a sweep group
    """

    print("Parsing " + run_dir)

    # Re-verify that all run components have exited cleanly by
    # checking their codar.workflow.return.[rc_name] file.
    # This includes internally spawned RCs such as sos_flow.
    # First, get the names of run-components by reading the
    # codar.cheetah.fobs.json file.

    # Open fob json file
    fob_dict = {}
    fob_json_filename = run_dir + "/" + "codar.cheetah.fob.json"
    try:
        with open(fob_json_filename, 'r') as f:
            fob_dict = json.load(f)
    except:
        print("ERROR: Could not read file " + fob_json_filename)
        return

    # sos_flow sees exes in the perf data that it collects. Form a mapping of 
    # rc_exe:rc_name so that we can get the rc_name from the exe from sos data.
    rc_name_exe = {}

    # Get rc names
    rc_names = []
    for rc in fob_dict['runs']:
        rc_names.append(rc['name'])
        
        # sos_flow sees an rc exe as
        # '/var/opt/cray/alps/spool/16406362/xgc-es+tau', whereas cheetah sees
        # '/lustre/atlas/proj-shared/csc143/kmehta/xgc/xgc-es+tau'. That is,
        # the exe paths are different.
        # So, just get the rc_exe name and not the path as the key.
        # e.g. "xgc-es+tau":"xgc" 
        rc_name_exe[rc['exe'].split("/")[-1]] = rc['name']

    # Open status return files for all RCs to verify they exited cleanly
    for rc in rc_names:
        return_code_file = run_dir + "/" + "codar.workflow.return." + rc
        if not Path(return_code_file).is_file():
            print("WARN: Could not find file " + return_code_file +
                  ". Skipping run directory.")
            return
        with open(return_code_file) as f:
            line = f.readline()
            ret_code = int(line.strip())
            if ret_code != 0:
                print("WARN: Run component " + rc +
                      " in " + run_dir + " did not exit cleanly. "
                      "Skipping run directory.")
                return

    # Now form dict of user codes and run params by reading
    # codar.cheetah.run-params.json.
    try:
        run_params_json_filename = run_dir + "/" + \
                                   "codar.cheetah.run-params.json"
        with open(run_params_json_filename, "r") as f:
            run_params_dict = json.load(f)
    except:
        print("WARN: Could not open " + run_params_json_filename +
              ". Skipping " + run_dir)
        return

    # Serialize nested dict and add to list of parsed run dicts
    serialized_run_params = __serialize_params_nested_dict(run_params_dict)

    # Append the node layout info from codar.cheetah.fob.json
    for rc_layout_d in (fob_dict.get('node_layout') or []):
        rc_name_layout = list(rc_layout_d.items())[0]
        if 'sosflow_aggregator' not in rc_name_layout[0]:
            serialized_run_params['node_layout_' + rc_name_layout[0]] \
                = rc_name_layout[1]
    
    # Add any new params discovered in this run dir to unique keys
    for key in serialized_run_params:
        unique_keys.add(key)

    # Run sosflow analysis on the run_dir now.
    sos_perf_results = sos_flow_analysis.sos_flow_analysis(run_dir)

    # keys in sos_perf_results are full exe paths. Get the rc name from
    # the exe path
    for rc_exe in sos_perf_results:
        rc_name = rc_name_exe[rc_exe]
        serialized_run_params[rc_name + "__time"] = \
            sos_perf_results[rc_exe]["time"]
        serialized_run_params[rc_name + "__adios_time"] = \
            sos_perf_results[rc_exe]["adios_time"]
        #serialized_run_params[rc_name + "__adios_data"] = \
        # sos_perf_results[rc_exe]["adios_data"]

        unique_keys.add(rc_name + "__time")
        unique_keys.add(rc_name + "__adios_time")
        #unique_keys.add(rc_name + "__adios_data")

    # Get the sizes of the output adios files.
    # The sizes were calculated by the post-processing function after the
    # run finished.
    # For every adios file, create columns named 'adios_file_1',
    # 'adios_file_2' etc. Set their value to the adios file name.
    # Then create columns named 'adios_file_1_size', 'adios_file_2_size'
    # etc. Set their value to the size of the file.

    # @TODO: The name of the file must be fetched from somewhere
    adios_filesizes_json = os.path.join(Path(run_dir),
                                        ".codar.cheetah.adios_sizes.out.json")
    if adios_filesizes_json.is_file():
        with open(adios_filesizes_json, 'r') as f:
            adios_sizes_d = json.load(f)
            file_count = 0
            for key, value in adios_sizes_d.items():
                file_count = file_count + 1
                new_key = "adios_file_" + str(file_count)
                serialized_run_params[new_key] = key
                unique_keys.add(new_key)

                size_key = new_key + "_size"
                serialized_run_params[size_key] = value
                unique_keys.add(size_key)
    
    # Get the output of du instead of reading sos data for output data size
    # This is hacky. I am assuming that this file contains output of du.
    # dir_size = -1
    # post_process_file = run_dir + "/codar.workflow.stdout.post-process"
    # if Path(post_process_file).is_file():
    #     f = open(post_process_file, "r")
    #     lines = f.readlines()
    #     lastline = lines[-1]
    #     dir_size = int(re.search(r'\d+', lastline).group())
    #
    # serialized_run_params["dir_size"] = dir_size
    # unique_keys.add("dir_size")
    
    serialized_run_params["run_dir"] = run_dir
    # print(serialized_run_params)
    # Add the performance results to list of parsed runs
    parsed_runs.append(serialized_run_params)


def __parse_sweep_group(sweep_group, parsed_runs, unique_keys):
    """
    Parse sweep group and get post-run performance information
    """

    # Check if group was run by checking if status file exists
    status_file = sweep_group + "/codar.workflow.status.json"
    if not Path(status_file).is_file():
        print("WARN: Could not find file " + status_file + 
              ". Skipping sweep group")
        return

    # Read status file
    try:
        with open(status_file, 'r') as f:
            status_json = json.load(f)
    except:
        print("ERROR: Could not read status file " + status_file)
        return

    # Get the return codes of all runs.
    # If any rc has failed, don't parse the run dir.
    successful_runs = []
    for run_dir, values in status_json.items():
        rc_return_codes = status_json[run_dir]['return_codes']
        for rc, rc_return_code in rc_return_codes.items():
            if rc_return_code != 0:
                break
        successful_runs.append(str(sweep_group) + "/" + run_dir)

    # Parse runs that have succeeded
    for run_dir in successful_runs:
        __parse_run_dir(run_dir, parsed_runs, unique_keys)


def generate_report(out_file_name="./campaign_results.csv"):
    """
    This is a post-run function.
    It walks the campaign tree and retrieves performance information
    about all completed runs.
    """

    # Ensure this is a campaign by checking for the presence of the
    # .campaign file
    assert (os.path.isfile("./.campaign")), "Current directory is not a " \
                                            "top-level campaign"
    
    # A list of dicts. Each dict contains metadata and performance information
    # about the run.
    parsed_runs = []
    
    # Unique application parameters that will be used as headers for csv output
    unique_keys = set()

    # Add run_dir as a key that will store the path to a run dir
    unique_keys.add("run_dir")

    subdirs = get_immediate_subdirs("./")
    
    for subdir in subdirs:
        print("Parsing campaign " + subdir)

        # Verify that current dir is a user-level campaign endpoint by
        # checking for the presence of the campaign-env.sh file.
        assert (os.path.isfile(os.path.join(subdir,"campaign-env.sh"))),\
            "Current directory is not a user-level campaign"

        # Walk through sweep groups
        sweep_groups = get_immediate_subdirs("./" + subdir)
        if not sweep_groups:
            print("No sweep groups found")
            return

        for sweep_group in sweep_groups:
            __parse_sweep_group("./" + subdir + "/" + sweep_group, parsed_runs,
                                unique_keys)

    # Write the parsed results to csv
    print("Done collecting performance information. Writing csv file.")
    with open(out_file_name, 'a') as f:
        dict_writer = csv.DictWriter(f, sorted(unique_keys))
        dict_writer.writeheader()
        dict_writer.writerows(parsed_runs)
