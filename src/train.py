#!/usr/bin/env python3
import torch
import argparse
import torch.nn as nn
import numpy as np
import multiprocessing
from torch.nn.utils.rnn import pad_sequence
from torch.nn import CTCLoss
from torchaudio.transforms import MFCC
from torch.utils.data import DataLoader, random_split, RandomSampler
from torch.optim import Adam, SGD
from torchaudio.datasets import LIBRISPEECH
from models import ResNet, DilatedResNet
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
from evaluation import WER, CER, collapse, remove_blanks

dictionary = "ABCDEFGHIJKLMNOPQRSTUVWXYZ' "


def text_to_tensor(text, dictionary=dictionary):
  """
  This function will convert a string of text
  to a tensor of character indicies in the given dictionary.
  The indicies will start from 1, as 0 means the blank
  character.
  """
  return torch.tensor([
      dictionary.index(c) + 1 if c in dictionary else 0
      for c in list(text.upper())
  ])


def tensor_to_text(tensor, dictionary=dictionary):
  return "".join(["-" if i == 0 else dictionary[i - 1] for i in tensor])


mfcc = MFCC(n_mfcc=64)


def waveforms_to_padded_mfccs(waveforms):
  mfccs = [mfcc(wave) for wave in waveforms]
  mfcc_lenghts = torch.tensor([mfcc.shape[2] for mfcc in mfccs])
  padded_mfccs = pad_sequence([mfcc.T for mfcc in mfccs],
                              batch_first=True).permute(0, 3, 2, 1)
  return padded_mfccs, mfcc_lenghts


def encode_utterances(utterances):
  encodings = torch.cat([text_to_tensor(utterance) for utterance in utterances])
  encoding_lengths = torch.tensor([len(utterance) for utterance in utterances])
  return encodings, encoding_lengths


def pad_collate(datapoints):
  waveforms, _, utterances, *rest = zip(*datapoints)

  return waveforms, utterances


def get_model(model):
  if model == "DilatedResNet":
    return DilatedResNet
  elif model == "ResNet":
    return ResNet
  else:
    return Basic


def train(data_path="../data",
          dataset="dev-clean",
          num_epochs=10,
          batch_size=32,
          parallel=False,
          device_name=None,
          load=None,
          save=None,
          model=None,
          log_dir="runs",
          num_workers=multiprocessing.cpu_count()):

  dataset = LIBRISPEECH(data_path, dataset, download=True)
  test_size = len(dataset) // 10
  val_size = 100
  rest_dataset, train_dataset = random_split(
      dataset, [test_size + val_size,
                len(dataset) - (test_size + val_size)])

  val_dataset, test_dataset = random_split(rest_dataset, [val_size, test_size])

  val_dataloader = DataLoader(
      val_dataset,
      sampler=RandomSampler(val_dataset, replacement=True),
      batch_size=16,
      collate_fn=pad_collate,
      num_workers=num_workers)

  test_dataloader = DataLoader(
      test_dataset,
      batch_size=batch_size,
      collate_fn=pad_collate,
      num_workers=num_workers)

  train_dataloader = DataLoader(
      train_dataset,
      batch_size=batch_size,
      shuffle=True,
      collate_fn=pad_collate,
      num_workers=num_workers)

  device = torch.device(device_name) if device_name else torch.device(
      "cuda" if torch.cuda.is_available() else "cpu")

  n_classes = len(dictionary) + 1
  Net = get_model(model)
  original_model = Net(n_classes)
  original_model = original_model.to(device)
  model = nn.DataParallel(original_model) if parallel else original_model

  if load:
    print(f"Loading model parameters from: {load}")
    model.load_state_dict(torch.load(load))

  optimizer = Adam(model.parameters(), lr=1e-3)
  loss_fn = CTCLoss()
  print(f"Using device: {device}")

  writer = SummaryWriter(log_dir)

  n_iter = 0

  for epoch in range(num_epochs):
    print(f"Training epoch: {epoch+1}")
    tqdm_train_dataloader = tqdm(train_dataloader)
    for i, (waveforms, utterances) in enumerate(tqdm_train_dataloader):
      n_iter += 1
      model.train()

      # First we zero our gradients, to make everything work nicely.
      optimizer.zero_grad()

      X, X_lengths = waveforms_to_padded_mfccs(waveforms)
      y, y_lengths = encode_utterances(utterances)

      X = X.to(device)
      X_lengths = X_lengths.to(device)
      y = y.to(device)

      # We predict the outputs using our model
      # and reshape the data to size (T, N, C) where
      # T is target length, N is batch size and C is number of classes.
      # In our case that is the length of the dictionary + 1
      # as we also need one more class for the blank character.
      pred_y = model(X)
      pred_y = pred_y.permute(2, 0, 1)
      pred_y_lengths = original_model.forward_shape(X_lengths)

      loss = loss_fn(pred_y, y, pred_y_lengths, y_lengths)
      loss.backward()
      optimizer.step()

      if n_iter % 50 == 0:
        model.eval()  # Set model in evaluation mode. Disabled dropout, etc.
        with torch.no_grad():  # Don't calculate gradients.

          train_loss = loss.item()

          waveforms, utterances = next(iter(val_dataloader))

          X, X_lengths = waveforms_to_padded_mfccs(waveforms)
          y, y_lengths = encode_utterances(utterances)

          X = X.to(device)
          X_lengths = X_lengths.to(device)
          y = y.to(device)

          pred_y = original_model(X)
          pred_y = pred_y.permute(2, 0, 1)
          pred_y_lengths = original_model.forward_shape(X_lengths)

          loss = loss_fn(pred_y, y, pred_y_lengths, y_lengths)

          val_loss = loss.item()
          val_texts_real = utterances
          val_texts_pred = [
              remove_blanks(collapse(tensor_to_text(tensor[:l])))
              for tensor, l in zip(
                  pred_y.permute(1, 0, 2).argmax(dim=2), pred_y_lengths)
          ]
          val_CER = np.mean([
              CER(input, target)
              for input, target in zip(val_texts_real, val_texts_pred)
          ])
          val_WER = np.mean([
              WER(input, target)
              for input, target in zip(val_texts_real, val_texts_pred)
          ])

          writer.add_scalar("Loss/Train", train_loss, n_iter)
          writer.add_scalar("Loss/Val", val_loss, n_iter)

          writer.add_scalar("Evaluation/CER", val_CER, n_iter)
          writer.add_scalar("Evaluation/WER", val_WER, n_iter)

          writer.add_text("Real", ", ".join(val_texts_real), n_iter)
          writer.add_text("Predicted", ", ".join(val_texts_pred), n_iter)

  if save:
    print(f"Saving model to: {save}")
    torch.save(model.state_dict(), save)

  writer.close()


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description="ASR Model Trainer")
  parser.add_argument(
      "--data-path", type=str, help="Path for data", default="../data")
  parser.add_argument(
      "--dataset", type=str, help="Dataset name", default="dev-clean")
  parser.add_argument("--device-name", type=str, help="Device name")
  parser.add_argument("--batch-size", type=int, help="Batch size", default=32)
  parser.add_argument(
      "--parallel",
      type=bool,
      nargs="?",
      const=True,
      help="Train in parallel",
      default=False)
  parser.add_argument(
      "--num-epochs",
      type=int,
      help="Number of epochs to train for",
      default=10)
  parser.add_argument(
      "--num-workers", type=int, help="How many workers to use", default=None)
  parser.add_argument(
      "--load", type=str, help="Load model parameters", default=None)

  parser.add_argument(
      "--save", type=str, help="Save model parameters", default=None)

  parser.add_argument("--model", type=str, help="Model", default=None)
  parser.add_argument(
      "--log-dir", type=str, help="Directory to save logs", default=None)

  args = parser.parse_args()

  train(
      data_path=args.data_path,
      dataset=args.dataset,
      device_name=args.device_name,
      batch_size=args.batch_size,
      parallel=args.parallel,
      num_epochs=args.num_epochs,
      load=args.load,
      save=args.save,
      log_dir=args.log_dir,
      num_workers=args.num_workers,
      model=args.model)
