```module load cuda/12.4.1-fasrc01
export CPLUS_INCLUDE_PATH=$CPLUS_INCLUDE_PATH:${HOME}/cuda-12.0/targets/x86_64-linux/include
module load gcc/12.2.0-fasrc01```


Small edits in microxcaling. 
* microxcaling: 
* Tatm: data/datasets.py



systems-scaling/
│── microxcaling/                     # Folder containing datasets
│   ├──             # 
│   ├──             # Processed/cleaned data
│   └── README.md             # Documentation for data
│── nanoGPT/                  # nanoGPT (model) code
│   ├── main.py               #  script
│   ├── utils.py              # Utility functions
│   ├── models/               # 
│   └── __init__.py           
│── olmo/                  # OLMo (model) code
│   ├── main.py               #  script
│   ├── utils.py              # Utility functions
│   ├── models/               # 
│   └── __init__.py           
│── notebooks/                # Jupyter notebooks for analysis
│── configs/                  # Configuration files
│── requirements.txt          # Dependencies
│── README.md                 # Project documentation
│── .gitignore            



Downstream evaluation
```bash
    from olmo.eval.downstream import *
    tokenizer = Tokenizer.from_file("olmo/tokenizers/allenai_eleuther-ai-gpt-neox-20b-pii-special.json")
    for x in label_to_task_map.values():
        print(x)
        kwargs = {}
        if isinstance(x, tuple):
            x, kwargs = x
        x(tokenizer=tokenizer, **kwargs)
    ```