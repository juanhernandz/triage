import logging
import traceback
from functools import partial
from pebble import ProcessPool
from multiprocessing.reduction import ForkingPickler

from triage.component.catwalk.utils import Batch

from triage.experiments import ExperimentBase


class MultiCoreExperiment(ExperimentBase):
    def __init__(self, config, db_engine, *args, n_processes=1, n_db_processes=1, **kwargs):
        try:
            ForkingPickler.dumps(db_engine)
        except Exception as exc:
            raise TypeError(
                "multiprocessing is unable to pickle passed SQLAlchemy engine. "
                "use triage.create_engine instead when running MultiCoreExperiment: "
                "(e.g. from triage import create_engine)"
            ) from exc

        super(MultiCoreExperiment, self).__init__(config, db_engine, *args, **kwargs)
        if n_processes < 1:
            raise ValueError("n_processes must be 1 or greater")
        if n_db_processes < 1:
            raise ValueError("n_db_processes must be 1 or greater")
        if n_db_processes == 1 and n_processes == 1:
            logging.warning(
                "Both n_processes and n_db_processes were set to 1. "
                "If you only wish to use one process to run the experiment, "
                "consider using the SingleThreadedExperiment class instead"
            )
        self.n_processes = n_processes
        self.n_db_processes = n_db_processes

    def generated_chunked_parallelized_results(
        self, partially_bound_function, tasks, n_processes, chunksize=1
    ):
        with ProcessPool(n_processes, max_tasks=1) as pool:
            future = pool.map(
                partially_bound_function,
                [list(task_batch) for task_batch in Batch(tasks, chunksize)],
            )
            iterator = future.result()
            while True:
                try:
                    yield next(iterator)
                except StopIteration:
                    break
                except Exception:
                    logging.exception('Child failure')

    def process_train_test_tasks(self, tasks):
        partial_test = partial(
            run_task_with_splatted_arguments, self.model_train_tester.process_task
        )

        serial_tasks, parallel_tasks = self.model_train_tester.split_tasks_by_parallelizability(tasks)
        # some tasks we want to run serially because they implement parallelization themselves
        # others we want to parallelize via multiprocessing
        logging.info("Starting parallel train/testing with %s processes", self.n_processes)
        parallelize(partial_test, parallel_tasks, self.n_processes)
        logging.info("Cleaned up concurrent pool")

        for serial_task in serial_tasks:
            final_task = serial_task
            # if n_jobs is not set we want to use the whole machine with -1
            # however if they set n_jobs we want to respect that
            if "n_jobs" not in serial_task["train_kwargs"]["parameters"]:
                logging.info("n_jobs not configured for serial task, "
                             "defaulting to -1 for entire machine")
                final_task["train_kwargs"]["parameters"]["n_jobs"] = -1
            self.model_train_tester.process_task(**final_task)

    def process_query_tasks(self, query_tasks):
        logging.info("Processing query tasks with %s processes", self.n_db_processes)
        for table_name, tasks in query_tasks.items():
            logging.info("Processing features for %s", table_name)
            self.feature_generator.run_commands(tasks.get("prepare", []))
            partial_insert = partial(
                insert_into_table, feature_generator=self.feature_generator
            )

            insert_batches = [
                list(task_batch) for task_batch in Batch(tasks.get("inserts", []), 25)
            ]
            parallelize(partial_insert, insert_batches, n_processes=self.n_db_processes)
            self.feature_generator.run_commands(tasks.get("finalize", []))
            logging.info("%s completed", table_name)

    def process_matrix_build_tasks(self, matrix_build_tasks):
        partial_build_matrix = partial(
            run_task_with_splatted_arguments, self.matrix_builder.build_matrix
        )
        logging.info(
            "Starting parallel matrix building: %s matrices, %s processes",
            len(self.matrix_build_tasks.keys()),
            self.n_processes,
        )
        parallelize(
            partial_build_matrix, self.matrix_build_tasks.values(), self.n_processes
        )


def insert_into_table(insert_statements, feature_generator):
    try:
        logging.info("Beginning insert batch")
        feature_generator.run_commands(insert_statements)
        return True
    except Exception:
        logging.error("Child error: %s", traceback.format_exc())
        return False


def parallelize(partially_bound_function, tasks, n_processes):
    num_successes = 0
    num_failures = 0
    results = []
    with ProcessPool(n_processes, max_tasks=1) as pool:
        future = pool.map(partially_bound_function, tasks)
        iterator = future.result()
        results = []
        while True:
            try:
                result = next(iterator)
            except StopIteration:
                break
            except Exception:
                logging.exception('Child failure')
                num_failures += 1
            else:
                results.append(result)
                num_successes += 1

        logging.info("Done. successes: %s, failures: %s", num_successes, num_failures)
        return results


def run_task_with_splatted_arguments(task_runner, task):
    try:
        return task_runner(**task)
    except Exception:
        logging.error("Child error: %s", traceback.format_exc())
        return None
