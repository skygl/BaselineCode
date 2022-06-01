import os
import argparse


def open_file(path):
    with open(path) as f:
        lines = f.readlines()
    return list(map(lambda x: x.rstrip(), list(filter(None, lines))))


def write_file(path, lines):
    with open(path, 'w') as f:
        f.writelines("\n".join(lines))


def traverse_dir(base_dir):
    text_list = []
    ner_list = []

    for name in os.listdir(base_dir):
        cur_path = os.path.join(base_dir, name)
        if os.path.isdir(cur_path):
            text, ner = traverse_dir(cur_path)
            text_list.extend(text)
            ner_list.extend(ner)
        else:
            if not name.endswith(".words"):
                continue

            ner_path = os.path.join(os.path.join(base_dir, f'{name.split(".")[0]}.ner'))
            if not os.path.exists(ner_path):
                raise "NER file not exists!"
            print(cur_path)

            text = open_file(cur_path)
            ner = open_file(ner_path)

            if len(text) != len(ner):
                raise "text count does not match with ner count!"

            text_list.extend(text)
            ner_list.extend(ner)

    return text_list, ner_list


def get_main_ext_parser():
    parser = argparse.ArgumentParser(allow_abbrev=False)

    # file_path
    parser.add_argument('--in_dir', type=str, default='./')

    return parser


if __name__ == "__main__":
    main_parser = get_main_ext_parser()
    main_args, _ = main_parser.parse_known_args()

    in_dir = main_args.in_dir

    text_, ner_ = traverse_dir(in_dir)

    write_file(os.path.join(in_dir, "ner.ner"), ner_)
    write_file(os.path.join(in_dir, "words.words"), text_)

    print(f"Total Count : {len(ner_)}")
