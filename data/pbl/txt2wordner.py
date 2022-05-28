import re


def change(w):
    return "-".join(reversed(w.split("-")))


def convert2wordner(raw_text):
    word, ner = raw_text.replace("\n", "").split("\t")
    ner = re.sub(r"[A-z\-]+", lambda m: change(m.group()), ner)
    return word, ner


def convert(lines):
    word_list, ner_list = [], []
    for line in lines:
        w, n = convert2wordner(line)
        word_list.append(w)
        ner_list.append(n)
    return word_list, ner_list


def save_txt(lines, path):
    with open(path, 'w') as f:
        f.writelines("\n".join(lines))


if __name__ == "__main__":
    train_file = open("./train.txt", "rt")
    test_file = open("./test.txt", "rt")

    train_lines = train_file.readlines()
    test_lines = test_file.readlines()

    train_word, train_ner = convert(train_lines)
    save_txt(train_word, "./train.words")
    save_txt(train_ner, "./train.ner")

    test_word, test_ner = convert(test_lines)
    save_txt(test_word, "./test.words")
    save_txt(test_ner, "./test.ner")
