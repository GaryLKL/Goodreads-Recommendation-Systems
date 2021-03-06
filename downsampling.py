import pyspark
from pyspark.sql import SparkSession
from pyspark.sql.types import StructType, StructField, IntegerType, StringType
from pyspark.sql.functions import monotonically_increasing_id, col, create_map, lit, row_number
from pyspark.sql.window import Window
from pyspark.sql.functions import rand
import pyspark.sql.functions as F
from itertools import chain
import argparse


def settings(memory):
    # setting
    conf = pyspark.SparkConf() \
        .setAll([('spark.app.name', 'downsampling code'),
                 ('spark.master', 'local'),
                 ('spark.executor.memory', memory),
                 ('spark.driver.memory', memory)])
    spark = SparkSession.builder \
        .config(conf=conf) \
        .getOrCreate()
    return spark


def get_frequent_user(data, user="user_id", threshold=20):
    '''
    This function is to remove those users who have low interactions
    (less than the threshold)
    Input:
    1. data
    2. user: the user column
    3. threshold: remove the users who have interactions lower than this threshold
    '''
    # 1. initialize n_users and n_samples
    n_users = data.select(user).distinct().count()
    n_samples = data.count()
    # 2. count the interaction and filter the users
    user_id_frequent = data.groupBy(user).count().filter("count>=" + str(threshold)).select(user)
    # print the percentage of the user_id which is removed
    print("I remove {0}% of the total users who have less than {1} iteractions.".
          format(str(round((1 - user_id_frequent.count() / n_users) * 100, 2)), threshold))
    # 3. then delete the users with less count (by joining on the user_id which was not removed from the last step)
    #data_freq = data.join(user_id_frequent, user, how='inner').select(data.schema.names)
    # print("I remove {}% of the total rows by deleting the users which have less than {} iteractions.".\
    #	  format(str(round((1-data_freq.count()/n_samples)*100, 2)), threshold))
    return user_id_frequent


def downsampling(data, user_df, user="user_id", percentage=0.01):
    '''
    This function is to keep k% of the users in the data
    Input:
    1. data
    2. user_df: a one-column DataFrame which only contains user_id
    3. user: the user column
    4. percentage: keep x percent of the users
    '''
    user_id_1_perc = user_df.sample(False, float(percentage), seed=123)
    downsample_data = data.join(user_id_1_perc, user, how='inner').select(data.schema.names)
    print("After downsampling, we only keep {0}% of the high-interation users. Now, we have {1} rows and {2} users.".
          format(float(percentage) * 100, downsample_data.count(), downsample_data.select(user).distinct().count()))
    return downsample_data


def create_repeated_index(data, col_name):
    '''
    This function is to transform the string column to an indexed integer column
    Input:
    1. data
    2. col_name: the column which would become an index column
    '''
    indexer = data.select(col_name).distinct() \
        .withColumn(col_name + "_index",
                    row_number()
                    .over(
                        Window.orderBy(
                            monotonically_increasing_id())
                    )
                    )
    data = data.join(indexer, col_name)
    return data


def create_row_index(data):
    '''
    This function is to a row index column.
    Input:
    1. data
    '''
    data = data.withColumn("row_index",
                           monotonically_increasing_id()
                           .cast(IntegerType())
                           )
    return data


def create_subset(data, threshold=500, percentage=0.01, user="user_id", item="book_id"):
    '''
    This function is to remove some users with low-frequent interactions and
    downsample the dataframe since 100% of the data is too big for the system
    Input:
    1. data
    2. threshold: users with less than k interactions would be removed
    3. percentage: the percentage of the users we are going to keep by sampling
    '''
    # 1. remove users with lower interactions
    print("Removing lower-interaction users.")
    freq_user = get_frequent_user(data=data, user=user, threshold=threshold)
    # 2. downsampling with x% of the users from data_freq table
    print("Downsampling the users. Only keeping " + str(int(percentage * 100)) + "%.")
    final_data = downsampling(data=data, user_df=freq_user, user=user, percentage=percentage)
    # 3. add user_id_index, book_id_index, row_id to the dataset
    return final_data


def create_schema():
    data_schema = StructType([
        StructField("user_id", IntegerType()),
        StructField("book_id", IntegerType()),
        StructField("is_read", IntegerType()),
        StructField("rating", IntegerType()),
        StructField("is_reviewed", IntegerType())  # for the data on HDFS
    ])
    # StructField("is_review", IntegerType()), for the data on local
    return data_schema


def set_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from_net_id", help="Inputing the netID for reading data")
    parser.add_argument("--to_net_id", help="Inputing the netID for saving models")
    parser.add_argument("--read_parquet_path", help="Specifying the path of the parquet file you want to read.")
    parser.add_argument("--write_parquet_path", help="Specifying the path of the parquet file you want to write.")
    parser.add_argument("--thres", help="Delete the users with less than thres (k) interactions.")
    parser.add_argument("--percentage", help="Downsampling the table with only k% of the user left.")
    parser.add_argument("--set_memory", help="Specifying the memory.")
    args = parser.parse_args()
    return args


if __name__ == "__main__":

    ### input arguments ###
    args = set_arguments()

    ### setting ###
    spark = settings(args.set_memory)

    # path
    from_hdfs_path = "hdfs:///user/" + args.from_net_id + "/goodreads/"
    to_hdfs_path = "hdfs:///user/" + args.to_net_id + "/goodreads/"

    # hdfs_path = "" # for local testing

    ### 1. read the parquet file ###
    #file = "subset_interactions.parquet"
    print("Reading the file.")
    data_schema = create_schema()

    data = spark.read.schema(data_schema).parquet(from_hdfs_path + "data/" + args.read_parquet_path)
    # repartition data
    #data = data.repartition(40)

    ### 2. downsampling ###
    print("Downsampling the dataframe.")
    downsample_data = create_subset(data=data, threshold=args.thres, percentage=float(args.percentage))
    #downsample_data = create_subset_with_index(data=data, threshold=500, percentage=float(0.01))
    ### 3. create user_id_index and book_id_index (IntegerType) ###
    # index columns will be useful during training
    #print("Creating index columns.")
    #downsample_data = index_func(data=downsample_data, col_name="user_id")
    #downsample_data = index_func(data=downsample_data, col_name="book_id")

    ### 4. write out downsample_data ###
    print("Writing the downsampling file.")
    data_schema = create_schema()
    #downsample_data.write.option("schema", data_schema).parquet(to_hdfs_path+"data/"+args.write_parquet_path, mode="overwrite")
    downsample_data.write.parquet(to_hdfs_path + "data/" + args.write_parquet_path, mode="overwrite")
    print("Finish outputing the subset.")
