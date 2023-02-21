import json
import os
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Union
from copy import deepcopy

import arrow
import tqdm
import pandas as pd
import numpy as np

import news_signals.signals as signals
import news_signals.newsapi as newsapi
from news_signals.data import aylien_ts_to_df, arrow_to_aylien_date
from news_signals.aql_builder import params_to_aql
from news_signals.log import create_logger


logger = create_logger(__name__, level=logging.INFO)

MAX_BODY_TOKENS = 500
DEFAULT_METADATA = {
    'name': 'News Signals Dataset'
}


class SignalsDataset:
    def __init__(self, signals=None, metadata=None):
        if metadata is None:
            metadata = {
                # default dataset name
                'name': 'News Signals Dataset'
            }
        else:
            assert 'name' in metadata, 'Dataset metadata must specify a name.'
        self.metadata = metadata

        if signals is None:
            signals = {}
        if type(signals) is not dict:
            signals = {s.id: s for s in signals}
            assert len(set([s.ts_column for s in signals.values()])) == 1, \
                'All signals in a dataset must have the same `ts_column` attribute.'
        self.signals = signals

    def update(self):
        raise NotImplementedError
        
    @classmethod
    def load(cls, dataset_path):        
        dataset_path = Path(dataset_path)
        dataset_signals = signals.Signal.load(dataset_path)
        if (dataset_path / 'metadata.json').is_file():
            metadata = read_json(dataset_path / 'metadata.json')
        else:
            metadata = None
        return cls(
            signals=dataset_signals,
            metadata=metadata
        )

    def save(self, dataset_path, overwrite=False):        
        dataset_path = Path(dataset_path)
        dataset_path.mkdir(parents=True, exist_ok=overwrite)
        for signal in self.signals.values():
            signal.save(dataset_path)
        write_json(
            self.metadata,
            dataset_path / 'metadata.json'
        )
        logger.info(
            f'Saved {len(self.signals)} signals in dataset to {dataset_path}.'
        )
    
    def aggregate_signal(self, name=None):
        if name is None:
            name = self.metadata['name']
        return signals.AggregateSignal(
            name=name,
            components=list(self.signals.values())
        )

    def plot(self, savedir=None):
        plot = self.aggregate_signal().plot()
        if savedir is not None:
            savedir = Path(savedir)
            savedir.mkdir(parents=True, exist_ok=True)
            fig = plot.get_figure()
            plot_file = savedir / f'{self.metadata["name"]}.png'
            fig.savefig(plot_file)
            logger.info(f"Saved plot to {plot_file}.")
        return plot
    
    def df(self, axis=0):
        """
        Return a long form view of all the signals in the dataset.
        TODO: memoize when signals are the same between calls
        """
        return pd.concat(
            [s.df for s in self.signals.values()],
            axis=axis
        )
    
    def corr(self, **kwargs):
        """
        Compute pairwise correlation of signals in the dataset.
        """
        return self.aggregate_signal().corr(**kwargs)
    
    def __getattr__(self, name):
        """
        Try to delegate to pandas if the attribute is not found on SignalsDataset.
        """
        try:
            df = self.df(axis=0)
            return getattr(df, name)
        except AttributeError as e:
            raise AttributeError(
                f"type object 'SignalsDataset' has no attribute '{name}'"
            )
    
    def generate_report(self):
        """
        Generate a report containing summary statistics about the dataset.
        """
        pass
    
    def __len__(self):
        return len(self.signals)
    
    def __getitem__(self, key):
        return self.signals[key]
    
    def __iter__(self):
        return iter(self.signals)
    
    def __contains__(self, key):
        return key in self.signals
    
    def __repr__(self):
        return f"SignalsDataset({self.signals})"
    
    def __str__(self):
        return f"SignalsDataset({self.signals})"
    
    def items(self):
        return self.signals.items()
    
    def keys(self):
        return self.signals.keys()

    def values(self):
        return self.signals.values()
    
    def map(self, func):
        """
        Note this is embarassingly parallel, should 
        be done multithreaded
        """
        logger.info(
            f'applying function to {len(self)} signals in dataset'
        )
        for k, v in tqdm.tqdm(self.signals.items(), total=len(self)):
            self.signals[k] = func(v)

def read_json(filepath):
    with open(filepath) as f:
        obj = json.load(f)
    return obj


def write_json(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f)


def read_jsonl(filepath):
    with open(filepath) as f:
        for line in f:
            yield json.loads(line)


def write_jsonl(items, filepath, mode="a"):
    content = "\n".join([json.dumps(x) for x in items]) + "\n"
    with open(filepath, mode) as f:
        f.write(content)


def ask_rmdir(dirpath, msg, yes="y"):
    if dirpath.exists():
        if input(msg) == yes:
            shutil.rmtree(dirpath)


def make_query(params, start, end, period="+1DAY"):
    _start = arrow_to_aylien_date(arrow.get(start))
    _end = arrow_to_aylien_date(arrow.get(end))
    aql = params_to_aql(params)
    new_params = deepcopy(params)
    new_params.update({
        "published_at.start": _start,
        "published_at.end": _end,
        "period": period,
        "language": "en",
        "aql": aql,
    })
    return new_params


def reduce_story(s):
    body = " ".join(s["body"].split()[:MAX_BODY_TOKENS])
    smart_cats = extract_smart_tagger_categories(s)
    reduced = {
        "title": s["title"],
        "body": body,
        "id": s["id"],
        "published_at": s["published_at"],
        "language": s["language"],
        "url": s["links"]["permalink"],
        "smart_tagger_categories": smart_cats,
    }
    return reduced


def extract_smart_tagger_categories(s):
    category_items = []
    for c in s["categories"]:
        if c["taxonomy"] == "aylien":
            item = {
                "score": c["score"],
                "id": c["id"]
            }
            category_items.append(item)
    return category_items


def read_last_timestamp(filepath):
    """
    Identifies last bucket's timestamp from buckets_*.jsonl file.
    """
    if filepath.exists():
        timestamps = [
            arrow.get(b["timestamp"]).datetime
            for b in read_jsonl(filepath)
        ]
        last = max(timestamps, key=arrow.get)
        return last
    else:
        return None


def retrieve_and_write_stories(
    params_template: Dict,
    start: datetime,
    end: datetime,
    ts: List,
    output_path: Path,
    num_stories: int = 20,
    stories_endpoint=newsapi.retrieve_stories
):
    time_to_volume = dict(
        (arrow.get(x["published_at"]).datetime, x["count"]) for x in ts
    )

    params_template['per_page'] = num_stories
    date_range = signals.Signal.date_range(start, end)
    start_end_tups = [
        (s, e) for s, e in zip(list(date_range), list(date_range)[1:])
    ]
    last_time = read_last_timestamp(output_path)
    passed_last = False

    for start, end in tqdm.tqdm(start_end_tups):

        if start == last_time:
            passed_last = True
        if last_time is not None and start <= last_time:
            continue
        # just sanity-checking that we observed last date in loop
        assert last_time is None or passed_last

        vol = time_to_volume[start]
        if vol > 0:
            params = make_query(params_template, start, end)
            stories = stories_endpoint(params)
            stories = [reduce_story(s) for s in stories]
        else:
            stories = []
        output_item = {
            "timestamp": str(start),
            "stories": stories,
            "volume": vol
        }
        write_jsonl([output_item], output_path, "a")


def retrieve_and_write_timeseries(
    params,
    start,
    end,
    output_path,
    ts_endpoint=newsapi.retrieve_timeseries
) -> List:
    if not output_path.exists():
        params = make_query(params, start, end)
        ts = ts_endpoint(params)
        write_json(ts, output_path)
    else:
        ts = read_json(output_path)
    return ts


def df_from_jsonl_buckets(path):
    story_bucket_records = []
    for b in read_jsonl(path):
        item = {"timestamp": b["timestamp"], "stories": b["stories"]}
        story_bucket_records.append(item)
    df = pd.DataFrame.from_records(
        story_bucket_records,
        index='timestamp'
    )
    return df


def signal_exists(signal, dataset_output_dir):
    return any(
        [f.name.startswith(signal.id) for f in dataset_output_dir.iterdir()]
    )


def generate_dataset(
    input: Union[List[signals.Signal], Path],
    output_dataset_dir: Path,
    start: datetime,
    end: datetime,
    id_field: str = "",
    name_field: str = "",
    stories_per_day: int = 20,
    overwrite: bool = False,
    delete_tmp_files: bool = False,
    stories_endpoint=newsapi.retrieve_stories,
    ts_endpoint=newsapi.retrieve_timeseries,
):

    """
    Turn list of signals into a dataset by populating each signal with time
    series and stories using Aylien Newsapi endpoints.
    The dataset is stored in an SqliteDict database.
    """

    if isinstance(input, Path):
        # this CSV should have a Wikidata ID and name for each entity
        df = pd.read_csv(input)
        signals_ = []
        for x in df.to_dict(orient="records"):

            name = x.get(name_field) or x[id_field]
            id = x[id_field]
            signal = signals.AylienSignal(
                name=name,
                params={"entity_ids": [id]}
            )
            signals_.append(signal)
    else:
        signals_ = input

    if overwrite and output_dataset_dir.exists():
        ask_rmdir(
            output_dataset_dir,
            msg=f"Are you sure you want to delete {output_dataset_dir} and "
            "start building dataset from scratch (y|n)? ",
        )
    output_dataset_dir.mkdir(parents=True, exist_ok=True)    

    for signal in tqdm.tqdm(signals_):
        if signal_exists(signal, output_dataset_dir):
            logger.info("signal exists already, skipping to next")
            continue

        stories_path = (
            output_dataset_dir / f"buckets_{signal.id}.jsonl"
        )
        ts_path = output_dataset_dir / f"timeseries_{signal.id}.jsonl"

        # TODO: pick a surface form vs. ID, or both
        params = signal.params

        # we save TS and stories to make continuation of the 
        # dataset generation process easier if it gets interrupted
        # by an error.
        ts = retrieve_and_write_timeseries(
            params, start, end, ts_path,
            ts_endpoint=ts_endpoint
        )
        retrieve_and_write_stories(
            params,
            start, end,
            ts,
            stories_path,
            num_stories=stories_per_day,
            stories_endpoint=stories_endpoint
        )

        # now this signal is completely realized
        stories_df = df_from_jsonl_buckets(stories_path)
        ts_df = aylien_ts_to_df({"time_series": ts}, dt_index=True)        
        signal.timeseries_df = ts_df
        signal.feeds_df = stories_df
        signal.save(output_dataset_dir)

        # delete temporary files
        if delete_tmp_files:
            ts_path.unlink()
            stories_path.unlink()

    return SignalsDataset.load(output_dataset_dir)
