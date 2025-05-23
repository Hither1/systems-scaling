import glob

data_path = "/n/holyscratch01//Lab/data"
data_path_2 = "/n/netscratch//data"
# Tokenized data
DATA_DICT = {
    "fineweb-1T": f"{data_path_2}/fineweb-recent-1T-merged",
    "fineweb-1T-val": f"{data_path_2}/fineweb-recent-1T-val-merged",
    "fineweb-100b": f"{data_path_2}/fineweb-100B-merged",
    "fineweb-100b-val": f"{data_path_2}/fineweb-100B-val-merged",
    "c4": f"{data_path}/c4-tokenized-llama",
    "c4-val": f"{data_path}/c4-val-tokenized-llama",
    "redpajama-val": f"/n/holylfs06/LABS/kempner_shared/Everyone/testbed/text/redpajama-v1/tokenized/t5-base/arxiv",
    "fineweb": "/n/holylfs06/LABS/kempner_shared/Everyone/testbed/text/fineweb-edu/tokenized/meta-llama-3/default",
    "starcoder": f"{data_path}/dolma-starcoder-tokenized-llama",
    "starcoder-val": f"{data_path}/dolma-starcoder-val-tokenized-llama",
    "proof-pile-2": f"{data_path}/proof-pile-2",
    "proof-pile-2-val": f"{data_path}/proof-pile-2-val",
    "fineweb-edu-100b": f"{data_path_2}/fineweb-edu-100B-merged",
    "fineweb-edu-100b-val": f"{data_path_2}/fineweb-edu-100B-val-merged",
    "slimpajama-chunk1": f"{data_path_2}/slimpajama-chunk1-tokenized",
    "slimpajama-chunk1-val": f"{data_path_2}/slimpajama-chunk1-val-tokenized",
    # Smollm: python -> 4B, fw -> 40B, owm -> 4B, cp2 -> 8B -> ~60B tokens
    "smollm-corpus": f"{data_path_2}/smollm-corpus",
    "smollm-corpus-val": f"{data_path_2}/smollm-corpus-val",
    "fineweb-nikhil-val": "/n/holylfs06/LABS/kempner_shared/Everyone/testbed/text/fineweb-edu/tokenized/meta-llama-2-sapphire-low-resource/val/",
}


# Pretrained model weights
MODEL_DICT = {}

# Datasets of scores from auxiliary models on c4
SCORE_DICT = {}


# Function to load all files with key as prefix as a list of paths
def load_score(key):
    keys = key.split("+")
    paths = []
    for key in keys:
        score_prefix = SCORE_DICT.get(key, key)
        paths.extend(glob.glob(f"{score_prefix}*/score"))
    return paths


INDEX_DICT = {}
