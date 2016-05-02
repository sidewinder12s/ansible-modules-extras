#!/usr/bin/python
#
# This is a free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This Ansible library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this library.  If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = """
---
module: elastic_beanstalk
short_description: Manage environments in Elastic Beanstalk.
description:
    - This module allows the user to create, terminate, or restart Elastic Beanstalk environments. >
        This module has a dependency on python-boto.
version_added: "2.0"
author: "Alec Disharoon (adisharoon-socialware)"
options:
  application_name:
    description:
      - The name of the Elastic Beanstalk application
    required: true
    default: null
  application_version:
    description:
      - The version of the Elastic Beanstalk application
    required: false
    default: null
  environment_name:
    description:
      - The name of the Elastic Beanstalk environment
    required: false
    default: Randomly generated
  cname:
    description:
      - The CNAME prefix to use for the Elastic Beanstalk environment
    required: false
    default: Randomly generated
  application_s3_bucket:
    description:
      - The S3 bucket where the application file (WAR, docker container, etc.) is located
    required: false
    default: null
  application_s3_key:
    description:
      - The S3 key for the application file (WAR, docker container, etc.)
    required: false
    default: null
  solution_stack:
    description:
      - The solution stack to use when creating an environment
    required: false
    default: null
  environment_options:
    description:
      - A list of environment option values as specified by the Elastic Beanstalk API-based CLI
    required: true
    default: null
  state:
    description:
      - The desired state of the EB environment
    required: true
    default: null
    choices: [ 'present', 'absent', 'restarted' ]
  redeploy:
    description:
      - When state is 'present' and the CNAME prefix is already in use, this parameter determines >
            whether to create a new environment and swap CNAMEs
    required: true
    default: false
extends_documentation_fragment: aws
"""

EXAMPLES = """
# Create an environment
 - elastic_beanstalk:
    application_name: my_application
    region: us-east-1
    application_version: 1.0
    cname: my_cname
    application_s3_bucket: my_bucket
    application_s3_key: my_war_file
    solution_stack: 64bit Amazon Linux 2015.03 v1.3.1 running Tomcat 8 Java 8
    environment_options:
      - Namespace: aws:autoscaling:launchconfiguration
        OptionName: InstanceType
        Value: t1.micro
    state: present

# Deploy a new application version to a new environment, and swap CNAMEs
# with the existing environment
 - elastic_beanstalk:
   application_name: my_application
   region: us-east-1
   application_version: 2.0
   cname: my_cname
   application_s3_bucket: my_bucket
   application_s3_key: my_war_file
   solution_stack: 64bit Amazon Linux 2015.03 v1.3.1 running Tomcat 8 Java 8
   environment_options:
     - Namespace: aws:autoscaling:launchconfiguration
       OptionName: InstanceType
       Value: t1.micro
   state: present
   redeploy: true

# Restart an environment
  - elastic_beanstalk:
      application_name: my_application
      region: us-east-1
      environment_name: my_environment
      state: restarted

# Terminate an environment
  - elastic_beanstalk:
    application_name: my_application
    region: us-east-1
    environment_name: my_environment
    state: absent
"""

import string
import time
import random

try:
    import boto
    import boto.beanstalk.exception
except ImportError:
    HAS_BOTO = False
else:
    HAS_BOTO = True

def retrieve_environment(module, beanstalk):
    """Return the dictionary that describes the environment from AWS, or None if not present"""

    application_name = module.params.get("application_name")
    environment_name = module.params.get("environment_name")

    try:
        environment_list = beanstalk.describe_environments(
            application_name=application_name,
            environment_names=[environment_name]
        )["DescribeEnvironmentsResponse"]["DescribeEnvironmentsResult"]["Environments"]
    except (boto.exception.BotoServerError, LookupError) as error:
        module.fail_json(msg="Unable to retrieve environment information: '{0}'".format(error))
    if len(environment_list) < 1:
        environment = None
    else:
        environment = environment_list[0]
        if environment["Status"] in ["Terminated"]:
            environment = None
    return environment


def make_present(module, beanstalk):
    """Ensures that the environment is present with supplied parameters.

    Returns unchanged if an exactly matching environment exists.  If not, creates the application
    and application version if they don't exist, then creates the environment.  If 'redeploy' is
    specified, the supplied CNAME will be swapped to the new environment.  The function will return
    a failure if any of these resources exist but are configured differently than specified.
    """

    application_name = module.params.get("application_name")
    application_version = module.params.get("application_version")
    environment_name = module.params.get("environment_name")
    cname = module.params.get("cname")
    application_s3_bucket = module.params.get("application_s3_bucket")
    application_s3_key = module.params.get("application_s3_key")
    solution_stack = module.params.get("solution_stack")
    redeploy = module.params.get("redeploy")
    environment_options = module.params.get("environment_options")

    changed = False
    if environment_options:
        environment_options = [
            (item["Namespace"], item["OptionName"], item["Value"])
            for item in environment_options
        ]
    environment = retrieve_environment(module, beanstalk)
    if environment and not redeploy:
        return (changed, environment)

    ss_response = beanstalk.list_available_solution_stacks()["ListAvailableSolutionStacksResponse"]
    available_solution_stacks = ss_response["ListAvailableSolutionStacksResult"]["SolutionStacks"]
    if not solution_stack:
        module.fail_json(msg="A solution stack must be provided.")
    if solution_stack not in available_solution_stacks:
        module.fail_json(
            msg=(
                "Solution stack '{0}' is not in the list "
                "of available solution stacks.".format(solution_stack)
            )
        )

    # create an EB application with supplied name; continue if one already exists.
    try:
        beanstalk.create_application(application_name)
    except (boto.beanstalk.exception.TooManyApplications,
            boto.exception.BotoServerError) as error:
        if error.message != "Application {0} already exists.".format(application_name):
            module.fail_json(
                msg="Unable to create Application '{0}': {1}".format(
                    application_name, error
                )
            )
    else:
        changed = True

    # Create an EB application version for some file stored in S3.
    # If a version already exists with the supplied name, break if it is not identical
    # to supplied parameters.
    try:
        beanstalk.create_application_version(
            application_name=application_name,
            version_label=application_version,
            s3_bucket=application_s3_bucket,
            s3_key=application_s3_key
        )
    except (boto.beanstalk.exception.TooManyApplicationVersions,
            boto.exception.BotoServerError) as error:
        if error.message == "Application Version {0} already exists.".format(application_version):
            av_response = beanstalk.describe_application_versions(
                application_name=application_name,
                version_labels=[application_version]
            )["DescribeApplicationVersionsResponse"]
            av_result = av_response["DescribeApplicationVersionsResult"]
            existing_version = av_result["ApplicationVersions"][0]
            existing_s3_bucket = existing_version["SourceBundle"]["S3Bucket"]
            existing_s3_key = existing_version["SourceBundle"]["S3Key"]
            if (application_s3_bucket != existing_s3_bucket or
                application_s3_key != existing_s3_key):
                module.fail_json(
                    msg=(
                        "S3 path for existing Application version '{0}' "
                        "does not match arguments supplied".format(application_version)
                    )
                )
        else:
            module.fail_json(
                msg=(
                    "Unable to create Application version"
                    "'{0}': {1}".format(application_version, error)
                )
            )
    else:
        changed = True

    # If CNAME is not used, just create a new environment with supplied parameters
    # If CNAME *is* already used, only create a new environment if we are swapping
    # that CNAME from the environment that uses it to our new environment
    # (i.e. the 'redeploy') parameter
    da_response = beanstalk.check_dns_availability(cname)["CheckDNSAvailabilityResponse"]
    da_result = da_response["CheckDNSAvailabilityResult"]
    if da_result["Available"]:
        try:
            beanstalk.create_environment(
                application_name=application_name,
                environment_name=environment_name,
                version_label=application_version,
                solution_stack_name=solution_stack,
                cname_prefix=cname,
                option_settings=environment_options
            )
        except (boto.beanstalk.exception.TooManyEnvironments,
                boto.beanstalk.exception.InsufficientPrivileges,
                boto.exception.BotoServerError) as error:
            module.fail_json(
                msg="Unable to create Environment '{0}': {1}".format(environment_name, error)
            )
        else:
            changed = True
            status = None
            while status != "Ready":
                time.sleep(10)
                status = retrieve_environment(module, beanstalk)["Status"]
    else:
        if redeploy:
            # Determine which environment is currently using the specified CNAME.
            try:
                environment_list = beanstalk.describe_environments(
                    application_name=application_name
                )["DescribeEnvironmentsResponse"]["DescribeEnvironmentsResult"]["Environments"]
            except (LookupError, TypeError, boto.exception.BotoServerError) as error:
                module.fail_json(
                    msg="Unable to retrieve environment information: '{0}'".format(error)
                )
            try:
                current_environment = next(
                    environment for environment in environment_list
                    if environment["CNAME"].split(".")[:1][0] == cname
                )
            except (LookupError, TypeError) as error:
                module.fail_json(
                    msg="Unable to derive CNAME prefix from environment: '{0}'".format(error)
                )

            # Create the new environment, without a CNAME specification, and wait for it to be ready
            try:
                beanstalk.create_environment(
                    application_name=application_name,
                    environment_name=environment_name,
                    version_label=application_version,
                    solution_stack_name=solution_stack,
                    option_settings=environment_options
                )
            except (boto.beanstalk.exception.TooManyEnvironments,
                    boto.beanstalk.exception.InsufficientPrivileges,
                    boto.exception.BotoServerError) as error:
                if error.message != "Environment {0} already exists.".format(environment_name):
                    module.fail_json(
                        msg="Unable to create Environment '{0}': {1}".format(
                            environment_name, error
                        )
                    )
            else:
                changed = True
                status = None
                while status != "Ready":
                    time.sleep(10)
                    status = retrieve_environment(module, beanstalk)["Status"]
                new_environment = retrieve_environment(module, beanstalk)

            # Swap the specified CNAME from the environment that has it to the new one.
            try:
                beanstalk.swap_environment_cnames(
                    source_environment_id=current_environment["EnvironmentId"],
                    source_environment_name=current_environment["EnvironmentName"],
                    destination_environment_id=new_environment["EnvironmentId"],
                    destination_environment_name=new_environment["EnvironmentName"]
                )
            except boto.exception.BotoServerError as error:
                module.fail_json(msg="Unable to swap CNAMEs: {0}".format(error))
            else:
                changed = True
                status = None
                while status != "Ready":
                    time.sleep(10)
                    status = retrieve_environment(module, beanstalk)["Status"]
        else:
            module.fail_json(msg="CNAME already in use and 'redeploy' parameter not specified.")
    return (changed, retrieve_environment(module, beanstalk))

def make_absent(module, beanstalk):
    """If the environment exists, terminate it."""

    application_name = module.params.get("application_name")
    environment_name = module.params.get("environment_name")
    changed = False

    if not retrieve_environment(module, beanstalk):
        module.fail_json(msg="Environment '{0}' not found for Application '{1}'.".format(
            environment_name,
            application_name
        ))

    try:
        beanstalk.terminate_environment(environment_name=environment_name)
    except (boto.beanstalk.exception.InsufficientPrivileges,
            boto.exception.BotoServerError) as error:
        module.fail_json(
            msg="Unable to terminate Environment '{0}': {1}".format(
                environment_name, error
            )
        )
    else:
        changed = True
        while retrieve_environment(module, beanstalk):
            time.sleep(10)
        environment = retrieve_environment(module, beanstalk)
    return (changed, environment)

def make_restarted(module, beanstalk):
    """If the environment exists, restart its app server, and wait for it to come up."""

    application_name = module.params.get("application_name")
    environment_name = module.params.get("environment_name")
    changed = False

    if not retrieve_environment(module, beanstalk):
        module.fail_json(msg="Environment '{0}' not found for Application '{1}'.".format(
            environment_name,
            application_name
        ))

    try:
        beanstalk.restart_app_server(environment_name=environment_name)
    except boto.exception.BotoServerError as error:
        module.fail_json(msg="Unable to restart application server: '{0}'".format(error))
    else:
        changed = True
        status = None
        while status != "Ready":
            time.sleep(10)
            status = retrieve_environment(module, beanstalk)["Status"]
        environment = retrieve_environment(module, beanstalk)
    return (changed, environment)

def main():
    """Set arguments, perform Ansible module setup, and call state implementation functions."""

    argument_spec = ec2_argument_spec()
    argument_spec.update(
        dict(
            application_name=dict(required=True),
            application_version=dict(),
            environment_name=dict(
                default="".join(
                    random.choice(string.ascii_letters + string.digits) for __ in range(10)
                )
            ),
            cname=dict(
                default="".join(
                    random.choice(string.ascii_letters + string.digits) for __ in range(10)
                )
            ),
            application_s3_bucket=dict(),
            application_s3_key=dict(),
            solution_stack=dict(),
            environment_options=dict(type="list"),
            state=dict(choices=["present", "absent", "restarted"], required=True),
            redeploy=dict(default=False, type="bool")
        )
    )
    module = AnsibleModule(
        argument_spec=argument_spec,
        mutually_exclusive=[[
            "configuration_template",
            "environment_options"
        ]]
    )
    if not HAS_BOTO:
        module.fail_json(msg='boto required for this module')
    region, ec2_url, aws_connect_kwargs = get_aws_connection_info(module)
    aws_connect_kwargs.pop("validate_certs")
    beanstalk = boto.beanstalk.connect_to_region(region, **aws_connect_kwargs)

    state = module.params.get("state")
    if state == "present":
        (changed, environment) = make_present(module, beanstalk)
    elif state == "absent":
        (changed, environment) = make_absent(module, beanstalk)
    elif state == "restarted":
        (changed, environment) = make_restarted(module, beanstalk)
    module.exit_json(changed=changed, environment=environment)

from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *
main()
