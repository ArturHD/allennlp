from collections import deque
import logging
from multiprocessing.process import BaseProcess
import random
import sys
import traceback
from typing import List, Iterator, Optional, Iterable, Union

import torch
import torch.multiprocessing as mp

from allennlp.common.util import lazy_groups_of, shuffle_iterable
from allennlp.common.tqdm import Tqdm
from allennlp.data.instance import Instance
from allennlp.data.data_loaders.data_loader import DataLoader, TensorDict, allennlp_collate
from allennlp.data.dataset_readers import DatasetReader, WorkerInfo
from allennlp.data.fields import TextField
from allennlp.data.samplers import BatchSampler
from allennlp.data.vocabulary import Vocabulary
from allennlp.nn.util import move_to_device


logger = logging.getLogger(__name__)


@DataLoader.register("multi_process")
class MultiProcessDataLoader(DataLoader):
    """
    The `MultiProcessDataLoader` is a [`DataLoader`](../data_loader/#dataloader)
    that's optimized for AllenNLP experiments.
    Unlike the [`PyTorchDataLoader`](../pytorch_data_loader/#pytorchdataloader),
    it can efficiently utilize multiple workers and always allows you to use a
    [`BatchSampler`](../../samplers/batch_sampler/#batchsampler).

    See
    [Using your reader with multi-process or distributed data loading](/api/data/dataset_readers/dataset_reader/#datasetreader.using_your_reader_with_multi-process_or_distributed_data_loading)
    for more information on how to optimize your `DatasetReader`.

    # Parameters

    reader: `DatasetReader`, required
        A `DatasetReader` used to load instances from the `data_path`.

    data_path: `str`, required
        Passed to `DatasetReader.read()`.

    batch_size: `int`, optional (default = `None`)
        When `batch_sampler` is unspecified, this option can be combined with `drop_last`
        and `shuffle` to control automatic batch sampling.

    drop_last: `bool`, optional (default = `False`)
        When `batch_sampler` is unspecified, this option can be combined with `batch_size`
        and `shuffle` to control automatic batch sampling.

        If `True`, the last batch will be dropped it doesn't contain a full `batch_size`
        number of `Instance`s.

    shuffle: `bool`, optional (default = `False`)
        When `batch_sampler` is unspecified, this option can be combined with `batch_size`
        and `drop_last` to control automatic batch sampling.

    batch_sampler: `BatchSampler`, optional (default = `None`)
        A `BatchSampler` to handle batching. This option is mutually exclusive with
        `batch_size`, `drop_last`, and `shuffle`.

    batches_per_epoch: `int`, optional (default = `None`)
        If specified, exactly `batches_per_epoch` batches will be generated with each call
        to `__iter__()`.

    num_workers: `int`, optional (default = `0`)
        The number of workers to use to read `Instance`s in parallel.
        If `num_workers = 0`, everything is done in the main process. Otherwise `num_workers`
        workers are forked or spawned (depending on the value of `start_method`), each of which
        calls `read()` on their copy of the `reader`.

        This means that in order for multi-process loading to be efficient when `num_workers > 1`,
        the `reader` needs to implement
        [`manual_multi_process_sharding`](/api/data/dataset_readers/dataset_reader/#datasetreader).

    max_instances_in_memory: `int`, optional (default = `None`)
        If not specified, all instances will be read and cached in memory for the duration
        of the data loader's life. This is generally ideal when your data can fit in memory
        during training. However, when your datasets are too big, using this option
        will turn on lazy loading, where only `max_instances_in_memory` instances are processed
        at a time.

        Note that this setting will affect how a `batch_sampler` is applied. If
        `max_instances_in_memory` is `None`, the sampler will be applied to all `Instance`s.
        Otherwise the sampler will be applied to only `max_instances_in_memory` `Instance`s
        at a time.

    start_method: `str`, optional (default = `"fork"`)
        The [start method](https://docs.python.org/3.7/library/multiprocessing.html#contexts-and-start-methods)
        used to spin up workers.

    pin_memory: `bool`, optional (default = `False`)
        When `True`, CPU tensors will be put into pinned (page-locked) memory, which results in faster copies to GPU.
        It also lets you make asyncronous copies to GPU by passing the `non_blocking=True` argument to
        `.to()` or `.cuda()`.

        See [the PyTorch docs](https://pytorch.org/docs/stable/notes/cuda.html#use-pinned-memory-buffers)
        for more info.

    device: `Optional[Union[int, str, torch.device]]`, optional (default = `None`)
        If given, batches will automatically be put on this device.

    !!! Note
        In a typical AllenNLP configuration file, the `reader` and `data_path` parameters don't
        get an entry under the "data_loader". The `reader` is constructed separately from
        the corresponding `dataset_reader` params, and the `data_path` is taken from the
        `train_data_path`, `validation_data_path`, or `test_data_path`.

    !!! Warning
        Multiprocessing code in Python is complicated! Especially code that involves lower-level libraries
        which may be spawning their own threads / processes. If you run into dead-locks while
        using `num_workers > 0`, luckily there are two simple work-arounds which usually fix the issue.

        The first work-around is to disable parallelism for these low-level libraries.
        For example, setting the environment variables `OMP_NUM_THREADS=1` and `TOKENIZERS_PARALLELISM=0`
        will do so for PyTorch and Numpy (for CPU operations) and HuggingFace Tokenizers, respectively.

        Alternatively, changing the `start_method` to "spawn" (when available, depending on your OS)
        may fix your issues without disabling parallelism for other libraries.

        See [issue #4848](https://github.com/allenai/allennlp/issues/4848) for more info.

    !!! Warning
        Another issue besides dead-locks that you could run into when using `num_workers > 0`
        is running out of shared memory, since tensors are passed between processes
        using shared memory, and some systems impose strict limits on the allowed size of shared
        memory.

        Luckily there is also a simple work-around for this. Either decrease `max_instances_in_memory`
        or increase your system's `ulimit`.

        See [issue #4847](https://github.com/allenai/allennlp/issues/4847) for more info.

    """  # noqa: E501

    def __init__(
        self,
        reader: DatasetReader,
        data_path: str,
        *,
        batch_size: int = None,
        drop_last: bool = False,
        shuffle: bool = False,
        batch_sampler: BatchSampler = None,
        batches_per_epoch: int = None,
        num_workers: int = 0,
        max_instances_in_memory: int = None,
        start_method: str = "fork",
        pin_memory: bool = False,
        device: Optional[Union[int, str, torch.device]] = None,
    ) -> None:
        # Do some parameter validation.
        if num_workers is not None and num_workers < 0:
            raise ValueError("num_workers cannot be a negative number")

        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        if batch_sampler is not None:
            if batch_size is not None:
                raise ValueError("batch_sampler option is mutually exclusive with batch_size")

            if drop_last:
                raise ValueError("batch_sampler option is mutually exclusive with drop_last")

            if shuffle:
                raise ValueError("batch_sampler option is mutually exclusive with shuffle")
        elif batch_size is None:
            raise ValueError("batch_size is required when batch_sampler is not supplied")

        if batches_per_epoch is not None and batches_per_epoch < 1:
            raise ValueError("batches_per_epoch must be at least 1")

        if max_instances_in_memory is not None:
            if batch_size is not None and max_instances_in_memory < batch_size:
                raise ValueError("max_instances_in_memory must be at least batch_size")
            elif max_instances_in_memory < 1:
                raise ValueError("max_instances_in_memory must be at least 1")

        if pin_memory and num_workers > 0 and start_method != "spawn":
            raise ValueError("start_method must be set to 'spawn' when using memory pinning")

        self.reader = reader
        self.data_path = data_path
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.shuffle = shuffle
        self.batch_sampler = batch_sampler
        self.batches_per_epoch = batches_per_epoch
        self.num_workers = num_workers
        self.collate_fn = allennlp_collate
        self.max_instances_in_memory = max_instances_in_memory
        self.start_method = start_method
        self.pin_memory = pin_memory
        self.device: Optional[torch.device] = None
        if device is not None:
            if not isinstance(device, torch.device):
                self.device = torch.device(device)
            else:
                self.device = device

        if (
            self.device is not None
            and self.device != torch.device("cpu")
            and num_workers
            and start_method != "spawn"
        ):
            raise ValueError(
                "start_method must be set to 'spawn' for data loader to put tensors onto a CUDA device"
            )

        # To make sure we have some backpressure in the worker queues we try to set
        # reasonable defaults for the maximum size of these queues.
        # They have to be big enough that is doesn't hurt performance, but small enough
        # that they don't take up too many resources when there is a bottleneck on the
        # consuming end of a queue.
        self._max_instance_queue_size = (
            None if max_instances_in_memory is None else max_instances_in_memory * 4
        )
        self._max_batch_queue_size = (
            None
            if max_instances_in_memory is None
            else 4 * max_instances_in_memory // (batch_size or 1)
        )

        # If max_instances_in_memory is not given, we'll keep a cache of all instances in this list.
        self._instances: Optional[List[Instance]] = None
        # Keeps track of state when `batches_per_epoch` is used.
        self._batch_generator: Optional[Iterator[TensorDict]] = None
        # For indexing instances.
        self._vocab: Optional[Vocabulary] = None

        if self.max_instances_in_memory is None:
            # Load all instances right away.
            deque(self.iter_instances(), maxlen=0)

    def index_with(self, vocab: Vocabulary) -> None:
        self._vocab = vocab
        if self._instances:
            for instance in self._instances:
                instance.index_fields(vocab)

    def __len__(self) -> int:
        if self.batches_per_epoch is not None:
            return self.batches_per_epoch
        elif self.max_instances_in_memory is None:
            # We haven't read the instances yet, so we do so now, caching them as we go.
            if not self._instances:
                deque(self.iter_instances(), maxlen=0)

            if self.batch_sampler is not None:
                return self.batch_sampler.get_num_batches(self._instances)  # type: ignore

            num_instances = len(self._instances)  # type: ignore
            # We know batch_size won't be None here since `batch_sampler` is None.
            batch_size: int = self.batch_size  # type: ignore
            if self.drop_last or num_instances % batch_size == 0:
                return num_instances // batch_size
            else:
                return 1 + num_instances // batch_size
        else:
            # We can't know the number of batches for a lazy loader when batches_per_epoch
            # is not specified.
            raise TypeError

    def __iter__(self) -> Iterator[TensorDict]:
        if self._vocab is None:
            raise ValueError(
                "This DataLoader has not been indexed with a Vocabulary yet. "
                "Did you forget to call DataLoader.index_with(vocab)?"
            )

        if self.batches_per_epoch is None:
            yield from self._iter_batches()
        else:
            if self._batch_generator is None:
                self._batch_generator = self._iter_batches()
            for i in range(self.batches_per_epoch):
                try:
                    yield next(self._batch_generator)
                except StopIteration:  # data_generator is exhausted
                    self._batch_generator = self._iter_batches()  # so refresh it
                    yield next(self._batch_generator)

    def iter_instances(self) -> Iterator[Instance]:
        if self._instances:
            yield from self._instances
        else:
            if self.max_instances_in_memory is None:
                self._instances = []

            if self.num_workers <= 0:
                # Just read all instances in main process.
                for instance in Tqdm.tqdm(
                    self.reader.read(self.data_path), desc="loading instances"
                ):
                    self.reader.apply_token_indexers(instance)
                    if self.max_instances_in_memory is None:
                        self._instances.append(instance)  # type: ignore
                    if self._vocab is not None:
                        instance.index_fields(self._vocab)
                    yield instance
            else:
                ctx = mp.get_context(self.start_method)
                queue: mp.JoinableQueue = (
                    ctx.JoinableQueue()
                    if self._max_instance_queue_size is None
                    else ctx.JoinableQueue(maxsize=self._max_instance_queue_size)
                )
                workers = self._start_instance_workers(queue, ctx)

                try:
                    for instance in Tqdm.tqdm(
                        self._gather_instances(queue), desc="loading instances"
                    ):
                        if self.max_instances_in_memory is None:
                            self._instances.append(instance)  # type: ignore
                        yield instance
                finally:
                    if hasattr(queue, "close"):  # for compat with different Python versions.
                        queue.close()  # type: ignore[attr-defined]
                    self._join_workers(workers, queue)

    def _iter_batches(self) -> Iterator[TensorDict]:
        if self._instances is not None or self.num_workers <= 0:
            for batch in self._instances_to_batches(self.iter_instances()):
                yield batch
        else:
            ctx = mp.get_context(self.start_method)

            queue: mp.JoinableQueue = (
                ctx.JoinableQueue()
                if self._max_batch_queue_size is None
                else ctx.JoinableQueue(maxsize=self._max_batch_queue_size)
            )
            workers = self._start_batch_workers(queue, ctx)

            try:
                # We can now start consuming from the `queue` as the batch workers
                # produce batches.
                done_count: int = 0
                while done_count < self.num_workers:
                    for batch, worker_error in iter(queue.get, (None, None)):
                        if worker_error is not None:
                            e, tb = worker_error
                            sys.stderr.write("".join(tb))
                            raise WorkerError(e)

                        yield batch
                        queue.task_done()
                    done_count += 1
            finally:
                if hasattr(queue, "close"):  # for compat with different Python versions.
                    queue.close()  # type: ignore[attr-defined]
                self._join_workers(workers, queue)

    def _start_instance_workers(self, queue: mp.JoinableQueue, ctx) -> List[BaseProcess]:
        workers: List[BaseProcess] = []
        for worker_id in range(self.num_workers):
            worker: BaseProcess = ctx.Process(
                target=self._instance_worker, args=(worker_id, queue), daemon=True
            )
            worker.start()
            workers.append(worker)
        return workers

    def _start_batch_workers(self, queue: mp.JoinableQueue, ctx) -> List[BaseProcess]:
        workers: List[BaseProcess] = []
        for worker_id in range(self.num_workers):
            worker: BaseProcess = ctx.Process(
                target=self._batch_worker, args=(worker_id, queue), daemon=True
            )
            worker.start()
            workers.append(worker)
        return workers

    def _join_workers(self, workers: List[BaseProcess], queue) -> None:
        # Each worker will be blocking on a call to `queue.join()`,
        # calling `queue.task_done()` times the number of workers will
        # call the `queue.join()` to return, and each worker should exit on its own.
        for _ in range(len(workers)):
            try:
                queue.task_done()
            except ValueError:
                # This happens if a worker died early.
                break
        # If for some reason the workers don't exit properly, we go through and terminate
        # them anyway.
        for worker in workers:
            if worker.is_alive():
                worker.terminate()

    def _instance_worker(self, worker_id: int, queue: mp.JoinableQueue) -> None:
        try:
            self.reader._set_worker_info(WorkerInfo(self.num_workers, worker_id))
            instances = self.reader.read(self.data_path)
            checked_for_token_indexers: bool = False
            for instance in instances:
                # Check the first instance to make sure it doesn't contain any TextFields with
                # token_indexers because we don't want to be duplicating those by sending
                # them across processes.
                if not checked_for_token_indexers:
                    for field_name, field in instance.fields.items():
                        if isinstance(field, TextField) and field._token_indexers is not None:
                            raise ValueError(
                                f"Found a TextField ({field_name}) with token_indexers already "
                                "applied, but you're using num_workers > 0 in your data loader. "
                                "Make sure your dataset reader's text_to_instance() method doesn't "
                                "add any token_indexers to the TextFields it creates. The token_indexers "
                                "should be added to the instances in apply_token_indexers() method of your "
                                "dataset reader (which you'll have to implement if you haven't done "
                                "so already)."
                            )
                    checked_for_token_indexers = True
                queue.put((instance, None))
        except Exception as e:
            queue.put((None, (str(e), traceback.format_exc())))

        # Indicate to the consumer that this worker is finished.
        queue.put((None, None))

        # Wait until this process can safely exit.
        queue.join()

    def _batch_worker(self, worker_id: int, queue: mp.JoinableQueue) -> None:
        try:
            self.reader._set_worker_info(WorkerInfo(self.num_workers, worker_id))
            instances = self.reader.read(self.data_path)
            for batch in self._instances_to_batches(instances):
                queue.put((batch, None))
        except Exception as e:
            queue.put((None, (str(e), traceback.format_exc())))

        # Indicate to the consumer (main thread) that this worker is finished.
        queue.put((None, None))

        # Wait until this process can safely exit.
        queue.join()

    def _gather_instances(self, queue: mp.JoinableQueue) -> Iterable[Instance]:
        done_count: int = 0
        while done_count < self.num_workers:
            for instance, worker_error in iter(queue.get, (None, None)):
                if worker_error is not None:
                    e, tb = worker_error
                    sys.stderr.write("".join(tb))
                    raise WorkerError(e)

                self.reader.apply_token_indexers(instance)
                if self._vocab is not None:
                    instance.index_fields(self._vocab)
                yield instance
                queue.task_done()
            done_count += 1

    def _index_instance(self, instance: Instance) -> Instance:
        self.reader.apply_token_indexers(instance)
        assert self._vocab is not None
        instance.index_fields(self._vocab)
        return instance

    def _instances_to_batches(self, instance_iterator: Iterable[Instance]) -> Iterator[TensorDict]:
        instance_iterator = (self._index_instance(instance) for instance in instance_iterator)

        if self.max_instances_in_memory is not None:
            max_instances_in_memory = max(
                1, self.max_instances_in_memory // max(self.num_workers, 1)
            )

            if max_instances_in_memory > 1 and self.batch_size is not None and self.batch_size > 1:
                # Make sure max_instances_in_memory is a multiple of `batch_size`.
                max_instances_in_memory = (
                    (max_instances_in_memory + self.batch_size - 1) // self.batch_size
                ) * self.batch_size

            if self.shuffle:
                instance_iterator = shuffle_iterable(
                    instance_iterator,
                    max_instances_in_memory,
                )

            instance_chunks: Iterable[List[Instance]] = lazy_groups_of(
                instance_iterator, max_instances_in_memory
            )
        else:
            # At this point we've already loaded the instances in memory and indexed them,
            # so this won't take long.
            instance_chunks = [list(instance_iterator)]
            if self.shuffle:
                random.shuffle(instance_chunks[0])

        for instances in instance_chunks:
            batches: Iterator[List[Instance]]
            if self.batch_sampler:
                batches = (
                    [instances[i] for i in batch_indices]
                    for batch_indices in self.batch_sampler.get_batch_indices(instances)
                )
            else:
                # NOTE: it's safe to assume `batch_size` is not `None` when `batch_sampler` is `None`.
                # Hence the `type: ignore` comment.
                batches = lazy_groups_of(instances, self.batch_size)  # type: ignore[arg-type]

            for batch in batches:
                if (
                    self.batch_sampler is None
                    and self.drop_last
                    and len(batch) < self.batch_size  # type: ignore[operator]
                ):
                    break
                tensor_dict = self.collate_fn(batch, pin_memory=self.pin_memory)
                if self.device is not None:
                    tensor_dict = move_to_device(tensor_dict, self.device)
                yield tensor_dict


class WorkerError(Exception):
    """
    An error raised when a worker fails.
    """

    pass
