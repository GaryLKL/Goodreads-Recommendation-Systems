import pyspark
from pyspark.sql import SparkSession
from pyspark.sql.window import Window
from pyspark.sql.types import StructType, StructField, IntegerType, StringType
from pyspark.ml.recommendation import ALS
from pyspark.mllib.evaluation import RankingMetrics
from pyspark.ml.evaluation import RegressionEvaluator
from pyspark.sql.functions import monotonically_increasing_id, col, expr
import pyspark.sql.functions as F
from functools import reduce
from pyspark.sql import DataFrame
from pyspark.ml.tuning import CrossValidator, ParamGridBuilder
from pyspark.ml.evaluation import Evaluator
import argparse
import numpy as np
from itertools import product
import time

def settings(memory):
	### setting ###
	conf = pyspark.SparkConf() \
			.setAll([('spark.app.name', 'downsampling code'),
					 ('spark.master', 'local'),
					 ('spark.executor.memory', memory),
					 ('spark.driver.memory', memory)])
	spark = SparkSession.builder \
			.config(conf=conf) \
			.getOrCreate()
	return spark

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


def customized_split_func(data, train, val_user, user="user_id", set_seed=123):
	'''
	This function is to hold out half of the interactions per user from validation to training.
	Output:
	1. row_index
	Input:
	1. data: the whole dataframe
	2. train: the training set dataframe which contains all columns
	3. val_user: the validation or testing set dataframe which only contains user_id
	4. user: name of the user columns
	'''
	# temporary validation table
	val_data_temp = data.join(val_user, on=user, how='inner').select(data.schema.names)
	# sampleBy: stratefied sampling; for each user_id, we extract half of the interaction
	val_frac = dict([(uid[0], 0.5) for uid in val_user.select(user).collect()])
	val_data = val_data_temp.sampleBy(user, val_frac, seed = set_seed)
	# using the subtract function and putting the other 50% to the training
	train_val_data = train.union(val_data_temp.subtract(val_data))
	return train_val_data, val_data

def unionAll(*dataframes):
	'''
	Thiss function is to concatenate a list of dataframes.
	refer to https://datascience.stackexchange.com/questions/11356/merging-multiple-data-frames-row-wise-in-pyspark
	'''
	return reduce(DataFrame.unionAll, dataframes)

def train_val_test_split(data, user="user_id"):
	'''
	If we don't perform k-fold cross validation, we just split the dataset into three subsets.
	'''
	# split by the users
	user_train_val_test_split = data.select(user).distinct().randomSplit([0.6, 0.2, 0.2], seed=123)
	# add half of the interactions to train from validation and testing
	train_data = data.join(
		user_train_val_test_split[0],
		on=user,
		how='inner'
		).select(data.schema.names) # user_split_sample is a 5-element list of user_id

	# in the validation set, leave half of the interactions per user to the training set
	train_data, val_data = customized_split_func(
		data=data,
		train=train_data,
		val_user=user_train_val_test_split[1],
		user=user
		)
	# in the test set, leave half of the interactions per user to the training set
	train_data, test_data = customized_split_func(
		data=data,
		train=train_data,
		val_user=user_train_val_test_split[2],
		user=user
		)
	return train_data, val_data, test_data

def tuning_als(train_data, val_data, rank_list=None, regParam_list=None,
			   metrics=None, k=10, maxIter=5, seed=123,
			   user="user_id", item="book_id", rating="rating"):
	'''
	This function is to run custom cross validation and metrics\
	Input:
	1. train_data: training data set
	2. val_data: validation data set
	3. rank_list: a list of ranks for tuning
	4. regParam_list: a list of regulization parameters for tuning
	5. train_data: train set
	6. val_data: validation set
	7. k: top k items for evaluation
	8. ranking_metrics: the function uses the ranking metrics if this is not False;
						{precisionAt, meanAveragePrecision, ndcgAt}
	9. regression_metrics: the function uses the regression metrics if this is not False;
							{rmse, mae, r2}
	output:
	1. best_param_dict: a dictionary of the best configuration
	2. tuning_table: a dictionary of all configurations
	'''
	if rank_list == None or regParam_list == None:
		print("Error! Please enter rank_list or regParam_list.")
		return
	if train_data == None or val_data == None:
		print("Error! You must input the data sets.")
		return
	if metrics == None:
		print("Error! You must select a metric.")
		return
	# tuning_table: for storing the hyperparameter and metrics
	tuning_table = {"rank": [],
					"regParam": [],
					metrics: []}

	# a combination of all tuning hyperparameters
	param_combination = list(product(rank_list, regParam_list))
	for i, params in enumerate(param_combination):
		print("Start " + str(i+1) + " configuration.")
		# initialize parameters, total_metrcs
		rank, regParam = params[0], params[1]

		# append rank and regParam into the tuning table
		tuning_table["rank"].append(rank)
		tuning_table["regParam"].append(regParam)

		# initializa, fit, transform the ALS model
		als = ALS(rank=rank, maxIter=maxIter, regParam = regParam, seed=123,
	              coldStartStrategy="drop", userCol=user,
	              itemCol=item, ratingCol=rating,
	              implicitPrefs=False, nonnegative=True)
		model = als.fit(train_data)
		val_pred = model.transform(val_data)
		# evaluation
		if metrics in ["rmse", "mae", "r2"]:
			# we use the regression metrics
			metrics_result = top_k_regressionmetrics(
								dataset=val_pred, k=k,
								regression_metrics=metrics,
								user=user, item=item, rating=rating,
								prediction="prediction")
		elif metrics in ["precisionAt", "meanAveragePrecision", "ndcgAt"]:
			# we use the ranking metrics
			metrics_result = top_k_rankingmetrics(
								dataset=val_pred, k=k,
								ranking_metrics=metrics,
								user=user, item=item, rating=rating,
								prediction="prediction")

		print("Finish " + str(i+1) + " configuration.")
		# append metrics into the tuning table
		tuning_table[metrics].append(round(metrics_result, 4))

	# find the best hyperparamters from the average metrics of k-fold
	best_param_dict = {}
	if metrics in ["rmse", "mae", "r2"]:
		# we use the regression metrics (select minimum)
		best_index = np.argmin(tuning_table[metrics])
	elif metrics in ["precisionAt", "meanAveragePrecision", "ndcgAt"]:
		# we use the ranking metrics (select maximum)
		best_index = np.argmax(tuning_table[metrics])

	# store the best configuration into the dictionary
	best_param_dict["rank"] = tuning_table["rank"][best_index]
	best_param_dict["regParam"] = tuning_table["regParam"][best_index]
	best_param_dict[metrics] = tuning_table[metrics][best_index]
	return best_param_dict, tuning_table

def top_k_rankingmetrics(dataset=None, k=10, ranking_metrics="precisionAt", user="user_id",
 						item="book_id", rating="rating", prediction="prediction"):
	'''
	This function is to compute the ranking metrics from predictions.
	Input:
	1. k: only evaluate the performance of the top k items
	2. ranking_metrics: precisionAt, meanAveragePrecision, ndcgAt
	3. user, item, prediction: column names; string type

	refer to https://vinta.ws/code/spark-ml-cookbook-pyspark.html
	'''
	if dataset == None:
		print("Error! Please specify a dataset.")
		return
	# prediction table
	windowSpec = Window.partitionBy(user).orderBy(col(prediction).desc())
	perUserPredictedItemsDF = dataset \
		.select(user, item, prediction, F.rank().over(windowSpec).alias('rank')) \
		.where('rank <= {}'.format(k)) \
		.groupBy(user) \
		.agg(expr('collect_list({}) as items'.format(item)))
	# actual target table
	windowSpec = Window.partitionBy(user).orderBy(col(rating).desc())
	perUserActualItemsDF = dataset \
		.select(user, item, rating, F.rank().over(windowSpec).alias('rank')) \
		.where('rank <= {}'.format(k)) \
		.groupBy(user) \
		.agg(expr('collect_list({}) as items'.format(item)))
	# join
	perUserItemsRDD = perUserPredictedItemsDF \
		.join(F.broadcast(perUserActualItemsDF), user, 'inner') \
		.rdd \
		.map(lambda row: (row[1], row[2]))
	ranking_metrics_evaluator = RankingMetrics(perUserItemsRDD)
	# get the result of the metric
	if ranking_metrics == "precisionAt":
		precision_at_k = ranking_metrics_evaluator.precisionAt(k)
		#print("precisionAt: {}".format(round(precision_at_k, 4)))
		return precision_at_k
	elif ranking_metrics == "meanAveragePrecision":
		mean_avg_precision = ranking_metrics_evaluator.meanAveragePrecision(k)
		#print("meanAveragePrecision: {}".format(round(mean_avg_precision, 4)))
		return mean_avg_precision
	elif ranking_metrics == "ndcgAt":
		ndcg_at_k = ranking_metrics_evaluator.ndcgAt(k)
		#print("meanAveragePrecision: {}".format(round(ndcg_at_k, 4)))
		return ndcg_at_k

def top_k_regressionmetrics(dataset=None, k=10, regression_metrics="rmse", user="user_id",
					 item="book_id", rating="rating", prediction="prediction"):
	'''
	This function is to compute the regression metrics from predictions
	Input:
	1. k: only evaluate the performance of the top k items
	2. regression_metrics: rmse, mae, r2
	3. user, item, prediction: column names; string type

	refer to https://spark.apache.org/docs/2.2.0/ml-collaborative-filtering.html
	'''
	if dataset == None:
		print("Error! Please specify a dataset.")
		return
	# prediction table
	windowSpec = Window.partitionBy(user).orderBy(col(prediction).desc())
	user_items_prediction_df = dataset \
		.select(user, item, prediction, rating, F.rank().over(windowSpec).alias('rank')) \
		.where('rank <= {}'.format(k))
	# regression metrics
	regression_metrics_evaluator = RegressionEvaluator(metricName=regression_metrics,
													   labelCol=rating,
                                					   predictionCol=prediction)
	result = regression_metrics_evaluator.evaluate(user_items_prediction_df)
	#print(result)
	#print("{}: {}".format(regression_metrics, round(result, 4)))
	return result # return rmse, mae, or r2

def set_arguments():
	parser = argparse.ArgumentParser()
	parser.add_argument("--from_net_id", help="Inputing the netID for reading data")
	parser.add_argument("--to_net_id", help="Inputing the netID for saving models")
	parser.add_argument("--parquet_path", help="Specifying the path of the parquet file you want to read.")
	parser.add_argument("--top_k", help="Only evaluating top k interations.")
	#parser.add_argument("--k_fold_split", help="Doing k-fold cross validation.")
	parser.add_argument("--metrics", help="The metrics for cross validation and measurement.")
	parser.add_argument("--rank_list", help="A list of ranks for tuning.")
	parser.add_argument("--regParam_list", help="A list of regularization parameters for tuning.")
	parser.add_argument("--path_of_model", help="Save the fitted model with this path.")
	parser.add_argument("--set_memory", help="Specifying the memory.")
	args = parser.parse_args()
	return args

if __name__ == "__main__":

	# arguments
	args = set_arguments()

	# initial some parameters from args
	top_k = int(args.top_k)
	my_metrics = args.metrics
	rank_list = eval(args.rank_list)
	regParam_list = eval(args.regParam_list)
	path_of_model = args.path_of_model
	#k_fold_split = int(args.k_fold_split)
	filename = args.parquet_path

	# setting
	spark = settings(args.set_memory)

	# path
	from_hdfs_path = "hdfs:///user/"+args.from_net_id+"/goodreads/"
	to_hdfs_path = "hdfs:///user/"+args.to_net_id+"/goodreads/"
	to_home_path = "/home/"+args.to_net_id+"/goodreads/"
	#hdfs_path = ""

	### 1. read data ###
	print("Reading the data.")
	data_schema = create_schema()
	data = spark.read.schema(data_schema).parquet(from_hdfs_path+"data/"+filename)
	# data = spark.read.parquet("indexed_poetry.parquet", schema=data_schema)

	### 2. split data ###
	print("Splitting the data set.")
	train_data, val_data, test_data = train_val_test_split(data)

	### 3. tuning ALS by cross validation ###
	start_time = time.time()

	print("Tuning the ALS model.")
	# cross validation tuning
	# rank_list = [5] # [5, 10, 15, 20]
	# regParam_list = [0.01] # np.logspace(start=-3, stop=2, num=6)
	tuning_result = tuning_als(
		train_data=train_data, val_data=val_data,
		rank_list=rank_list, regParam_list=regParam_list,
		k=top_k, maxIter=5, metrics=my_metrics
	)

	tuning_hist = tuning_result[1]
	best_config = tuning_result[0]
	best_rank, best_regParam = best_config["rank"], best_config["regParam"]

	### 4. prediction on the test set ###
	# after find the best hyperparameters, we train on the train set again, and then make prediction on the test set
	# union train_data and val_data together
	new_train_data = unionAll(*[train_data, val_data])

	# initialize ALS estimator
	print("Re-training on the train set and predicting on the test set.")
	als = ALS(rank=best_rank, regParam = best_regParam, maxIter=5,
			  seed=123, coldStartStrategy="drop", userCol="user_id",
              itemCol="book_id", ratingCol="rating",
              implicitPrefs=False, nonnegative=True)

	model = als.fit(new_train_data)
	test_pred = model.transform(test_data) # predictions is a DataFrame with prediction column
	test_pred.show(20)
	# compute ranking metrics on the test set
	if my_metrics in ["rmse", "mae", "r2"]:
		test_metrics = top_k_regressionmetrics(dataset=test_pred,
						k=top_k,
						regression_metrics=my_metrics,
						user="user_id",
						item="book_id",
						rating="rating",
						prediction="prediction")
	elif my_metrics in ["precisionAt", "meanAveragePrecision", "ndcgAt"]:
		test_metrics = top_k_rankingmetrics(dataset=test_pred,
						k=top_k,
						ranking_metrics=my_metrics,
						user="user_id",
						item="book_id",
						rating="rating",
						prediction="prediction")

	end_time = time.time()
	time_statement = "It takes {0} seconds to tune and train the model.".\
						format(str(round(end_time-start_time, 2)))
	print(time_statement)

	### 5. save the estimator (model) ###
	# refer to https://spark.apache.org/docs/2.3.0/api/python/pyspark.ml.html#pyspark.ml.classification.LogisticRegression.save
	print("Saving the estimator.")
	model.write().overwrite().save(to_hdfs_path+"models/"+path_of_model)

	# record all of the hyperparameter configurations, the best configuration, testing result
	print("Recording the tuning history.")
	with open(to_home_path+"history/"+"tuning_history.txt", "a+") as file:
		write_args = (path_of_model,
					  filename,
				   	  str(rank_list),
				   	  str(regParam_list),
				   	  str(tuning_hist),
				   	  best_rank,
				   	  best_regParam,
				   	  my_metrics,
				   	  round(test_metrics, 4),
				   	  time_statement)
		file.write("Model Path: {0}\n" \
				   "Data: {1}\n" \
				   "Rank List: {2}\n" \
				   "RegParam List: {3}\n" \
				   "Tuning History: {4}\n" \
				   "Best Rank: {5}; Best RegParam: {6}\n" \
				   "Test Result ({7}): {8}\n" \
				   "Note: {9}\n\n" \
				   "---------" \
				   "\n\n" \
				   .format(*write_args))
