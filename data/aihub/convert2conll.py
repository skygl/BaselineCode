import argparse
import json
import os
import json

from khaiii import KhaiiiApi
import khaiii


def open_json(path):
    with open(path, "r", encoding='utf-8-sig') as json_file:
        json_object = json.load(json_file)
    return json_object


def open_file(path):
    with open(path) as f:
        lines = f.readlines()
    return lines


def get_main_ext_parser():
    parser = argparse.ArgumentParser(allow_abbrev=False)

    # file_path
    parser.add_argument('--in_dir', type=str, default='./')

    return parser


def get_data(text: str, label: [object], plus_one=False):
    current_text = []
    current_labels = []

    start = 0
    end = -1
    prev_idx = -1
    for i, word in enumerate(text.split()):
        start = end + 1
        end = start + len(word)
        is_break = False

        for idx, label_ in enumerate(label):
            se = label_["begin"]
            le = label_["end"] + (1 if plus_one else 0)
            l = label_["type"]

            if se <= start < end <= le:
                current_text.append(word)
                current_labels.append(f"I-{l}" if prev_idx == idx else f"B-{l}")
                prev_idx = idx
                is_break = True
                break
            elif se <= start <= le < end:
                current_text.append(word[:-(end - le)])
                current_labels.append(f"I-{l}" if prev_idx == idx else f"B-{l}")
                current_text.append(word[-(end - le):])
                current_labels.append("O")
                prev_idx = -1
                is_break = True
                break
            elif start <= se < le <= end:
                current_text.append(word[:-(le - start)])
                current_labels.append("O")
                current_text.append(word[-(le - start):])
                current_labels.append(f"B-{l}")
                prev_idx = idx
                is_break = True
                break
            elif start <= se <= end <= le:
                current_text.append(word[:-(le - start)])
                current_labels.append("O")
                current_text.append(word[-(le - start):-(end - le)])
                current_labels.append(f"B-{l}")
                current_text.append(word[-(end - le):])
                current_labels.append("O")
                is_break = True
                break

        if not is_break:
            current_text.append(word)
            current_labels.append("O")

    return current_text, current_labels


def get_data_from_source(json_object):
    src_data_list = json_object["data"]

    text_list = []
    labels_list = []

    for src_data in src_data_list:
        try:
            text = src_data["text"]
            label = src_data["NE"]

            current_text, current_labels = get_data(text, label)

            text_list.append(" ".join(current_text))
            labels_list.append(" ".join(current_labels))

        except khaiii.KhaiiiExcept as e:
            continue
    return text_list, labels_list

def get_data_from_sentence_source(json_object):
    src_data_list = json_object["data"]

    text_list = []
    labels_list = []

    for src_data in src_data_list:
        sentences = src_data["sentence"]
        for sentence in sentences:
            text = sentence["text"]
            label = sentence["NE"]
            current_text, current_labels = get_data(text, label, plus_one=True)

            text_list.append(" ".join(current_text))
            labels_list.append(" ".join(current_labels))

    return text_list, labels_list

def get_data_from_label(json_object):
    data_list = json_object['data']

    text_list = []
    labels_list = []

    for data in data_list:
        for row in data["rows"]:
            try:
                text = row["text"]
                label = row["NE"]

                current_text, current_labels = get_data(text, label)

                text_list.append(" ".join(current_text))
                labels_list.append(" ".join(current_labels))

            except khaiii.KhaiiiExcept as e:
                continue

    return text_list, labels_list


def traverse_dir(base_dir):
    for name in os.listdir(base_dir):
        cur_path = os.path.join(base_dir, name)
        if os.path.isdir(cur_path):
            traverse_dir(cur_path)
        else:
            if not name.endswith(".json"):
                continue
            print(cur_path)
            json_object = open_json(cur_path)

            first = json_object['data'][0]

            if "sentence" in first.keys():
                text_list, labels_list = get_data_from_sentence_source(json_object)
            elif "NE" in first.keys():
                text_list, labels_list = get_data_from_source(json_object)
            else:
                text_list, labels_list = get_data_from_label(json_object)
            file_name = name[:-5]
            text_file_name = file_name + ".words"
            label_file_name = file_name + ".ner"

            with open(os.path.join(base_dir, text_file_name), 'w') as f:
                f.writelines("\n".join(text_list))

            with open(os.path.join(base_dir, label_file_name), 'w') as f:
                f.writelines("\n".join(labels_list))


if __name__ == "__main__":
    main_parser = get_main_ext_parser()
    main_args, _ = main_parser.parse_known_args()

    in_dir = main_args.in_dir

    api = KhaiiiApi()

    base_dir = in_dir

    traverse_dir(base_dir)
