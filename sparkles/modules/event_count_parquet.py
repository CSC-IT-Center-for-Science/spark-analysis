# Counts the number of events from start to end time in a given window of fixed interval
import h5py
from pyspark import SparkConf, SparkContext
from pyspark.sql.types import Row, StructField, StructType, StringType, IntegerType, LongType
from datetime import datetime, date, timedelta
import sys
from operator import add
from pyspark.sql import SQLContext
import os
import json
from sparkles.modules.utils.helper import saveFeatures
from os.path import dirname
import argparse
import time
import calendar


# Hash the keys into different interval periods
def keymod(x, start_time, interval):

    curr_t = x.created
    curr_t = curr_t - start_time
    keyindex = int(curr_t / interval)
    return (keyindex, 1)


# Transform the final time
def timetr(x, start_time, interval):

    dt = int(start_time + x[0] * interval)
    # t = (start_time + x[0] * interval) / 1000.0
    # dt = datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S.%f')  # x[0] is keyindex
    return (dt, x[1])  # x[1] is the total aggregated count


def saveResult(configstr, x, sqlContext, userdatadir, featureset_name, description, details, modulename, module_parameters, parent_datasets):

    schemaString = "timestamp count"

    fields_rdd = []
    for field_name in schemaString.split():
        if(field_name == 'count'):
            fields_rdd.append(StructField(field_name, IntegerType(), True))
        else:
            fields_rdd.append(StructField(field_name, LongType(), True))

    schema_rdd = StructType(fields_rdd)
    dfRdd = sqlContext.createDataFrame(x, schema_rdd)
    saveFeatures(configstr, dfRdd, userdatadir, featureset_name, description, details, modulename, json.dumps(module_parameters), json.dumps(parent_datasets))


def main():
    conf = SparkConf()
    conf.setAppName("Parquet Count 60")
    conf.set("spark.jars", "file:/shared_data/spark_jars/hadoop-openstack-3.0.0-SNAPSHOT.jar")
    sc = SparkContext(conf=conf)

    parser = argparse.ArgumentParser()
    parser.add_argument("backend", type=str)
    parser.add_argument("helperpath", type=str)
    parser.add_argument("shuffle_partitions", type=str)
    parser.add_argument("params", type=str)
    parser.add_argument("inputs", type=str)
    parser.add_argument("features", type=str, nargs='?')

    args = parser.parse_args()

    # Swift Connection
    if(args.backend == 'swift'):
        hadoopConf = sc._jsc.hadoopConfiguration()
        hadoopConf.set("fs.swift.impl", "org.apache.hadoop.fs.swift.snative.SwiftNativeFileSystem")
        hadoopConf.set("fs.swift.service.SparkTest.auth.url", os.environ['OS_AUTH_URL'] + "/tokens")
        hadoopConf.set("fs.swift.service.SparkTest.http.port", "8443")
        hadoopConf.set("fs.swift.service.SparkTest.auth.endpoint.prefix", "/")
        hadoopConf.set("fs.swift.service.SparkTest.region", os.environ['OS_REGION_NAME'])
        hadoopConf.set("fs.swift.service.SparkTest.public", "false")
        hadoopConf.set("fs.swift.service.SparkTest.tenant", os.environ['OS_TENANT_ID'])
        hadoopConf.set("fs.swift.service.SparkTest.username", os.environ['OS_USERNAME'])
        hadoopConf.set("fs.swift.service.SparkTest.password", os.environ['OS_PASSWORD'])

    helperpath = args.helperpath
    sc.addFile(helperpath + "/utils/helper.py")  # To import custom modules
    shuffle_partitions = args.shuffle_partitions

    params = json.loads(args.params)
    inputs = json.loads(args.inputs)
    features = json.loads(args.features)

    userdatadir = str(features['userdatadir'])
    description = str(features['description'])
    details = str(features['details'])
    featureset_name = str(features['featureset_name'])
    modulename = str(features['modulename'])
    configstr = str(features['configstr'])

    tablename = str(params['tablename'])

    start_time_str = str(params['start_time'])
    start_time = int(str(calendar.timegm(time.strptime(start_time_str[:-4], '%Y-%m-%d_%H:%M:%S'))) + start_time_str[-3:])  # convert to epoch

    end_time_str = str(params['end_time'])
    end_time = int(str(calendar.timegm(time.strptime(end_time_str[:-4], '%Y-%m-%d_%H:%M:%S'))) + end_time_str[-3:])  # convert to epoch

    interval = float(params['interval'])

    filepath = str(inputs[0])  # Provide the complete path
    filename = os.path.basename(os.path.abspath(filepath))
    tablepath = filepath + '/' + filename + '_' + str.lower(tablename) + '.parquet'

    sqlContext = SQLContext(sc)
    sqlContext.setConf("spark.sql.shuffle.partitions", shuffle_partitions)

    df = sqlContext.read.parquet(tablepath)

    df.registerTempTable(tablename)
    df = sqlContext.sql("SELECT created FROM " + tablename + " WHERE created <" + str(end_time) + " AND created >=" + str(start_time))

    rdd = df.map(lambda x: keymod(x, start_time, interval)).reduceByKey(add)
    rdd = rdd.sortByKey()
    rdd = rdd.map(lambda x: timetr(x, start_time, interval))  # Human readable time

    parent_datasets = []
    parent_datasets.append(filename)  # Just append the names of the dataset used not the full path (Fetched from metadata)
    saveResult(configstr, rdd, sqlContext, userdatadir, featureset_name, description, details, modulename, params, parent_datasets)

    print(rdd.collect())

    sc.stop()


if __name__ == "__main__":
    main()
