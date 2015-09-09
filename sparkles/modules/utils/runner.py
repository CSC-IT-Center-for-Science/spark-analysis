from sqlalchemy import text
from models import Base, config_to_db_session, Dataset, Analysis
from datetime import datetime
import getpass
# import re
from subprocess import call
import yaml
import os
from os.path import dirname
from swiftclient.service import SwiftService, SwiftUploadObject
import json
import warnings
import shutil
import socket


class SparkRunner(object):

    def __init__(self, configpath=None):

        config = None
        with open(configpath, 'r') as config_file:
            config = yaml.load(config_file)

            # Download the metadata from swift first
            options = {'os_auth_url': os.environ['OS_AUTH_URL'], 'os_username': os.environ['OS_USERNAME'], 'os_password': os.environ['OS_PASSWORD'], 'os_tenant_id': os.environ['OS_TENANT_ID'], 'os_tenant_name': os.environ['OS_TENANT_NAME']}
            swiftService = SwiftService(options=options)

            # Create the containers which are used in this application for Object Storage
            swiftService.post(container='containerFiles')
            swiftService.post(container='containerFeatures')
            swiftService.post(container='containerModules')

            out_file = config['DB_LOCATION']  # Where to store the metadata on local system
            localoptions = {'out_file': out_file}
            objects = []
            objects.append('sqlite.db')  # Name of the metadata file
            swiftDownload = swiftService.download(container='containerModules', objects=objects, options=localoptions)
            for downloaded in swiftDownload:
                if ('error' in downloaded.keys()):
                    print(downloaded['error'])
                    warnings.warn('The metadata was not found on backend, creating new! If this is not your first time usage after installing the library, please consult with the support team before proceeding!', RuntimeWarning)

        dburi = config['DATABASE_URI']
        self.clusterUrl = "spark://" + socket.gethostname() + ':' + config['CLUSTER_PORT']
        self.session = config_to_db_session(dburi, Base)
        self.config = config
        self.configpath = configpath

    def list_modules(self, prefix=''):

        ''' Searches for the modules according to the given prefix.
        If no prefix is given, it returns all the modules in the backend
        '''

        print('List of available modules:')
        print('--------------------------')

        analysisModules = self.session.query(Analysis).filter(Analysis.name.like('%' + prefix + '%')).all()
        for amodule in analysisModules:
            print('Name: ' + amodule.name + '|Description: ' + amodule.description + '|Details: ' + amodule.details)

    def list_datasets(self, prefix=''):

        ''' Searches for the datasets/featuresets according to the given prefix.
        If no prefix is given, it returns all the datasets/featuresets in the backend
        '''

        print('List of available datasets:')
        print('---------------------------')

        datasets = self.session.query(Dataset).filter(Dataset.name.like('%' + prefix + '%')).all()
        for dataset in datasets:
            if(dataset.module is None):
                print(dataset.name + '|Description: ' + dataset.description + '|Details: ' + dataset.details)
            else:
                print(dataset.name + '|Description: ' + dataset.description + '|Details: ' + dataset.details + '|Module used: ' + dataset.module.name + '|Parameters used: ' + dataset.module_parameters + '|Parents: ' + json.dumps(list(map((lambda x: x.name), dataset.parents))))
            print('****************************')

    def import_analysis(self, name='', description='', details='', filepath='', params='', inputs='', outputs=''):

        ''' Imports the analysis module (present on the local path) written in Spark RDD language in the backend.
        The backend is a Swift Object Store. Parameters are needed to update the metadata.
        '''

        filename = os.path.basename(filepath)
        created = datetime.now()
        user = getpass.getuser()

        checkMod = self.session.query(Analysis).from_statement(text("SELECT * FROM analysis where name=:name")).\
            params(name=name).first()

        if(checkMod is None):
            analysisMod = Analysis(name=name, filepath=filename, description=description, details=details, created=created, user=user, parameters=params, inputs=inputs, outputs=outputs)
            shutil.copyfile(self.config['DB_LOCATION'], '/shared_data/sparkles/tmp/sqlite_temp.db')  # Backup metadata

            self.session.add(analysisMod)
            self.session.commit()

            # Upload the metadata and module to swift
            options = {'os_auth_url': os.environ['OS_AUTH_URL'], 'os_username': os.environ['OS_USERNAME'], 'os_password': os.environ['OS_PASSWORD'], 'os_tenant_id': os.environ['OS_TENANT_ID'], 'os_tenant_name': os.environ['OS_TENANT_NAME']}
            swiftService = SwiftService(options=options)
            objects = []
            objects.append(SwiftUploadObject(self.config['DB_LOCATION'], object_name='sqlite.db'))
            objects.append(SwiftUploadObject(filepath, object_name=filename))

            swiftUpload = swiftService.upload(container='containerModules', objects=objects)
            for uploaded in swiftUpload:
                if("error" in uploaded.keys()):
                    shutil.copyfile('/shared_data/sparkles/tmp/sqlite_temp.db', self.config['DB_LOCATION'])
                    raise RuntimeError(uploaded['error'])
                print(uploaded)
        else:
            raise RuntimeError("Analysis " + name + " already exists")

    def run_analysis(self, modulename='', params=None, inputs=None, features=None):

        ''' Runs the given analysis module against the given input datasets and produces the output.
        The analysis module and the datasets have to be imported first (Metadata is read)
        The output can be printed on console or saved as a featureset (features parameter needs to be specified)
        '''

        if(modulename is None or params is None or inputs is None):
            raise RuntimeError("Modulename, params and inputs are necessary")
        else:
            analysisMod = self.session.query(Analysis).from_statement(text("SELECT * FROM analysis where name=:name")).\
                params(name=modulename).first()
            if(analysisMod):
                filepaths = ''
                filepathsarr = []

                # Download the module from swift first
                options = {'os_auth_url': os.environ['OS_AUTH_URL'], 'os_username': os.environ['OS_USERNAME'], 'os_password': os.environ['OS_PASSWORD'], 'os_tenant_id': os.environ['OS_TENANT_ID'], 'os_tenant_name': os.environ['OS_TENANT_NAME']}
                swiftService = SwiftService(options=options)

                out_file = self.config['MODULE_LOCAL_STORAGE'] + analysisMod.filepath
                localoptions = {'out_file': out_file}
                objects = []
                objects.append(analysisMod.filepath)
                swiftDownload = swiftService.download(container='containerModules', objects=objects, options=localoptions)

                for downloaded in swiftDownload:
                    if("error" in downloaded.keys()):
                        raise RuntimeError(downloaded['error'])
                    print(downloaded)

                for inputfile in inputs:
                    dataset = self.session.query(Dataset).from_statement(text("SELECT * FROM datasets where name=:name")).\
                        params(name=inputfile).first()
                    if(dataset):
                        filepathsarr.append(dataset.filepath)

                if(not filepathsarr):
                    raise RuntimeError("No datasets found")

                filepaths = json.dumps(filepathsarr)
                params = json.dumps(params)

                helperpath = dirname(dirname(os.path.abspath(__file__)))
                if(features is None):
                    call(["/opt/spark/bin/pyspark", out_file, "--master", self.clusterUrl, helperpath, params, filepaths])
                else:
                    features['configpath'] = self.configpath  # Pass the configpath as a default parameter
                    features = json.dumps(features)
                    call(["/opt/spark/bin/pyspark", out_file, "--master", self.clusterUrl, helperpath, params, filepaths, features])
            else:
                raise RuntimeError("Analysis module not found")

    def import_dataset(self, inputfiles=[], description='', details='', userdatadir=''):

        ''' Imports a given dataset (on a local path) to the backend which is a Swift object store
        Multiple files can be imported as inputfiles parameters is an array. The userdatadir is the object store container URI
        '''

        if(inputfiles):

            if(not userdatadir):
                userdatadir = 'swift://containerFiles.SparkTest'

            path = dirname(dirname(os.path.abspath(__file__)))
            configpath = self.configpath
            originalpaths = json.dumps(inputfiles)
            call(["/opt/spark/bin/pyspark", path + "/data_import.py", "--master", self.clusterUrl, originalpaths, description, details, userdatadir, configpath])

        else:
            raise RuntimeError("Please ensure inputfiles is not None or empty")
