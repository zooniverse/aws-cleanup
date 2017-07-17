#!/usr/bin/env python
import datetime
import re
import sys

from time import sleep

import boto3

node_names = (
    'anayltics',
    'current',
    'graylog',
    'legacy',
    'manifests',
    'multi-mongo',
    'multi-redis',
    'oldweather-dev',
    'ouroboros',
    'ouroboros-backup',
    'ouroboros-manifests',
    'ouroboros-mongodb1',
    'ouroboros-mongodb2',
    'ouroboros-mongodb3',
    'ouroboros-redis',
    'ouroboros-staging',
    'panoptes-api',
    'panoptes-api-emergency',
    'panoptes-api-staging',
    'panoptes-api-staging-emergency',
    'panoptes-cellect',
    'panoptes-cellect-staging',
    'panoptes-designator',
    'panoptes-designator-staging',
    'panoptes-dumpworker',
    'panoptes-dumpworker-staging',
    'panoptes-graylog',
    'panoptes-graylog-staging',
    'panoptes-kafka',
    'panoptes-kafka-staging',
    'panoptes-messenger-staging',
    'panoptes-messenger',
    'panoptes-mini-talk-search',
    'panoptes-redis',
    'panoptes-redis-staging',
    'panoptes-seven-ten',
    'panoptes-seven-ten-staging',
    'panoptes-sugar',
    'panoptes-sugar-staging',
    'panoptes-talk',
    'panoptes-talk-staging',
    'panoptes-zookeeper',
    'panoptes-zookeeper-staging',
    'php',
    'staging',
    'static',
)
min_age = datetime.timedelta(days=7)

name_regex = ('^(%s) [0-9]{4}-[0-9]{2}-[0-9]{2} '
              '[0-9]{2}\.[0-9]{2}\.[0-9]{2}$' % '|'.join(node_names))

autoscaling_client = boto3.client('autoscaling')

describe_auto_scaling_groups_paginator = autoscaling_client.get_paginator(
    'describe_auto_scaling_groups'
)

launch_configs_in_use = set()

for page in describe_auto_scaling_groups_paginator.paginate():
    for group in page['AutoScalingGroups']:
        launch_configs_in_use.add(group['LaunchConfigurationName'])

describe_launch_configs_paginator = autoscaling_client.get_paginator(
    'describe_launch_configurations'
)

amis_in_use = set()
now = datetime.datetime.now(datetime.timezone.utc)
now_no_tz = now.replace(tzinfo=None)

for page in describe_launch_configs_paginator.paginate():
    for lc in page['LaunchConfigurations']:
        if (
            re.match(name_regex, lc['LaunchConfigurationName'].strip())
            and (now - lc['CreatedTime']) > min_age
            and lc['LaunchConfigurationName'] not in launch_configs_in_use
           ):
            print("Deleting stale launch config {}".format(
                lc['LaunchConfigurationName'],
            ))
            autoscaling_client.delete_launch_configuration(
                LaunchConfigurationName=lc['LaunchConfigurationName'],
            )
            # Avoid rate limiting
            sleep(1)
        else:
            amis_in_use.add(lc['ImageId'])

ec2_client = boto3.client('ec2')

stale_amis = {}

for node in node_names:
    images = ec2_client.describe_images(
        Owners=['self'],
        Filters=[{
            'Name': 'tag:dockernode',
            'Values': [node],
        }],
    )

    for image in images['Images']:
        if not re.match(name_regex,  image['Name']):
            continue

        is_latest = False
        for tag in image['Tags']:
            if tag['Key'] == 'latest' and tag['Value'] == 'true':
                is_latest = True
                break
        if is_latest:
            continue

        try:
            image_created = datetime.datetime.strptime(
                image['Name'],
                node + ' %Y-%m-%d %H.%M.%S'
            )
        except ValueError:
            continue

        if (now_no_tz - image_created) > min_age:
            snapshot_ids = [
                device['Ebs']['SnapshotId']
                for device in image['BlockDeviceMappings']
                if 'Ebs' in device
            ]
            stale_amis[image['ImageId']] = snapshot_ids

for ami_id, snapshot_ids in stale_amis.items():
    if ami_id in amis_in_use:
        continue

    ec2_client.deregister_image(ImageId=ami_id)
    print("Deleted AMI: {}".format(ami_id))

    for snapshot_id in snapshot_ids:
        ec2_client.delete_snapshot(SnapshotId=snapshot_id)
        print("Deleted EBS Snapshot: {}".format(snapshot_id))
        # Avoid rate limiting
        sleep(1)

s3_client = boto3.client('s3')
min_age = datetime.timedelta(days=120)
list_objects_paginator = s3_client.get_paginator('list_objects_v2')

for page in list_objects_paginator.paginate(
    Bucket='zooniverse-code',
    Prefix='databases/',
):
    for database_backup in page['Contents']:
        date = database_backup['Key'].split('/')[1]
        try:
            date = datetime.datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            try:
                date = datetime.datetime.strptime(date, '%d%m%y')
            except ValueError:
                print("Warning: Unknown date format for {}".format(
                    database_backup['Key'],
                ))
                continue

        if (now_no_tz - date) > min_age and date.weekday() != 0:
            print("Deleting old backup: {}".format(database_backup['Key']))
            s3_client.delete_object(
                Bucket='zooniverse-code',
                Key=database_backup['Key'],
            )
            # Avoid rate limiting
            sleep(1)
