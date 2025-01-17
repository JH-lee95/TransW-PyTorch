from absl import app
from absl import flags
import data
import metric
import model as model_definition
import os
import storage
import torch
import torch.optim as optim
from torch.utils import data as torch_data
from torch.utils import tensorboard
from typing import Tuple
import json
from tqdm import tqdm


from transformers import BartForConditionalGeneration, BartTokenizer

FLAGS = flags.FLAGS
flags.DEFINE_float("lr", default=0.01, help="Learning rate value.")
flags.DEFINE_integer("seed", default=1234, help="Seed value.")
flags.DEFINE_integer("batch_size", default=256, help="Maximum batch size.")
flags.DEFINE_integer("validation_batch_size", default=128, help="Maximum batch size during model validation.")
flags.DEFINE_integer("vector_length", default=1024, help="Length of entity/relation vector.")
flags.DEFINE_float("margin", default=1.0, help="Margin value in margin-based ranking loss.")
flags.DEFINE_integer("norm", default=1, help="Norm used for calculating dissimilarity metric (usually 1 or 2).")
flags.DEFINE_integer("epochs", default=500, help="Number of training epochs.")
flags.DEFINE_string("dataset_path", default="./synth_data", help="Path to dataset.")
flags.DEFINE_bool("use_gpu", default=True, help="Flag enabling gpu usage.")
flags.DEFINE_integer("validation_freq", default=10, help="Validate model every X epochs.")
flags.DEFINE_string("checkpoint_path", default="", help="Path to model checkpoint (by default train from scratch).")
flags.DEFINE_string("tensorboard_log_dir", default="./runs", help="Path for tensorboard log directory.")

HITS_AT_1_SCORE = float
HITS_AT_3_SCORE = float
HITS_AT_10_SCORE = float
MRR_SCORE = float
METRICS = Tuple[HITS_AT_1_SCORE, HITS_AT_3_SCORE, HITS_AT_10_SCORE, MRR_SCORE]


def test(model: torch.nn.Module, data_generator: torch_data.DataLoader, entities_count: int,
         summary_writer: tensorboard.SummaryWriter, device: torch.device, epoch_id: int, metric_suffix: str,
         ) -> METRICS:
    examples_count = 0.0
    hits_at_1 = 0.0
    hits_at_3 = 0.0
    hits_at_10 = 0.0
    mrr = 0.0

    entity_ids = torch.arange(end=entities_count, device=device).unsqueeze(0)
    for head, relation, tail in data_generator:
        current_batch_size = head.size()[0]

        head, relation, tail = head.to(device), relation.to(device), tail.to(device)
        all_entities = entity_ids.repeat(current_batch_size, 1)
        heads = head.reshape(-1, 1).repeat(1, all_entities.size()[1])
        relations = relation.reshape(-1, 1).repeat(1, all_entities.size()[1])
        tails = tail.reshape(-1, 1).repeat(1, all_entities.size()[1])

        # Check all possible tails
        triplets = torch.stack((heads, relations, all_entities), dim=2).reshape(-1, 3)
        tails_predictions = model.predict(triplets).reshape(current_batch_size, -1)
        # Check all possible heads
        triplets = torch.stack((all_entities, relations, tails), dim=2).reshape(-1, 3)
        heads_predictions = model.predict(triplets).reshape(current_batch_size, -1)

        # Concat predictions
        predictions = torch.cat((tails_predictions, heads_predictions), dim=0)
        ground_truth_entity_id = torch.cat((tail.reshape(-1, 1), head.reshape(-1, 1)))

        hits_at_1 += metric.hit_at_k(predictions, ground_truth_entity_id, device=device, k=1)
        hits_at_3 += metric.hit_at_k(predictions, ground_truth_entity_id, device=device, k=3)
        hits_at_10 += metric.hit_at_k(predictions, ground_truth_entity_id, device=device, k=10)
        mrr += metric.mrr(predictions, ground_truth_entity_id)

        examples_count += predictions.size()[0]

    hits_at_1_score = hits_at_1 / examples_count * 100
    hits_at_3_score = hits_at_3 / examples_count * 100
    hits_at_10_score = hits_at_10 / examples_count * 100
    mrr_score = mrr / examples_count * 100
    summary_writer.add_scalar('Metrics/Hits_1/' + metric_suffix, hits_at_1_score, global_step=epoch_id)
    summary_writer.add_scalar('Metrics/Hits_3/' + metric_suffix, hits_at_3_score, global_step=epoch_id)
    summary_writer.add_scalar('Metrics/Hits_10/' + metric_suffix, hits_at_10_score, global_step=epoch_id)
    summary_writer.add_scalar('Metrics/MRR/' + metric_suffix, mrr_score, global_step=epoch_id)

    return hits_at_1_score, hits_at_3_score, hits_at_10_score, mrr_score


def main(_):
    torch.random.manual_seed(FLAGS.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


    tokenizer = BartTokenizer.from_pretrained("facebook/bart-large")
    lm = BartForConditionalGeneration.from_pretrained("facebook/bart-large", forced_bos_token_id=0)

    path = FLAGS.dataset_path
    train_path = os.path.join(path, "persona_train_all.txt")
    validation_path = os.path.join(path, "persona_valid.txt")
    test_path = os.path.join(path, "persona_test.txt")

    ent_dict_path=os.path.join(path,"entities.dict")
    rel_dict_path=os.path.join(path,"relation.dict")

    with open(ent_dict_path,"r") as f1:
      entity2id=json.load(f1)

    with open(rel_dict_path,"r") as f2:
      relation2id=json.load(f2)


    id2entity={v:k for k,v in entity2id.items()}
    id2relation={v:k for k,v in relation2id.items()}

    train_triples=data.create_triples(train_path)
    valid_triples=data.create_triples(validation_path)
    test_triples=data.create_triples(test_path)

    batch_size = FLAGS.batch_size
    vector_length = FLAGS.vector_length
    margin = FLAGS.margin
    norm = FLAGS.norm
    learning_rate = FLAGS.lr
    epochs = FLAGS.epochs
    device = torch.device('cuda') if FLAGS.use_gpu else torch.device('cpu')

    train_set = data.PersonaTripleDataset(entity2id, relation2id, train_triples)
    train_generator = torch_data.DataLoader(train_set, batch_size=batch_size)
    validation_set = data.PersonaTripleDataset(entity2id, relation2id, valid_triples)
    validation_generator = torch_data.DataLoader(validation_set, batch_size=FLAGS.validation_batch_size)
    test_set = data.PersonaTripleDataset(entity2id, relation2id, test_triples)
    test_generator = torch_data.DataLoader(validation_set, batch_size=FLAGS.validation_batch_size)

    model = model_definition.TransW(entity_count=len(entity2id), relation_count=len(relation2id),lm=lm,tokenizer=tokenizer,id2entity=id2entity,id2relation=id2relation, dim=vector_length,
                                    margin=margin,
                                    device=device, norm=norm)  # type: torch.nn.Module
    model = model.to(device)
    optimizer = optim.SGD(model.parameters(), lr=learning_rate)

    summary_writer = tensorboard.SummaryWriter(log_dir=FLAGS.tensorboard_log_dir)
    start_epoch_id = 1
    step = 0
    best_score = 0.0

    if FLAGS.checkpoint_path:
        start_epoch_id, step, best_score = storage.load_checkpoint(FLAGS.checkpoint_path, model, optimizer)

    print(model)

    # Training loop
    for epoch_id in tqdm(range(start_epoch_id, epochs + 1),desc="Epoch"):
        print("Starting epoch: ", epoch_id)
        loss_impacting_samples_count = 0
        samples_count = 0
        model.train()

        for h_id,r_id,t_id in tqdm(train_generator,desc="Batch Input"):
            local_heads, local_relations, local_tails = (h_id.to(device), r_id.to(device),
                                                         t_id.to(device))

            positive_triples = torch.stack((local_heads, local_relations, local_tails), dim=1)

            # Preparing negatives.
            # Generate binary tensor to replace either head or tail. 1 means replace head, 0 means replace tail.
            head_or_tail = torch.randint(high=2, size=local_heads.size(), device=device)
            random_entities = torch.randint(high=len(entity2id), size=local_heads.size(), device=device)
            broken_heads = torch.where(head_or_tail == 1, random_entities, local_heads)
            broken_tails = torch.where(head_or_tail == 0, random_entities, local_tails)
            negative_triples = torch.stack((broken_heads, local_relations, broken_tails), dim=1)

            optimizer.zero_grad()

            loss, pd, nd = model(positive_triples, negative_triples)
            loss.mean().backward()

            summary_writer.add_scalar('Loss/train', loss.mean().data.cpu().numpy(), global_step=step)
            summary_writer.add_scalar('Distance/positive', pd.sum().data.cpu().numpy(), global_step=step)
            summary_writer.add_scalar('Distance/negative', nd.sum().data.cpu().numpy(), global_step=step)

            loss = loss.data.cpu()
            loss_impacting_samples_count += loss.nonzero().size()[0]
            samples_count += loss.size()[0]

            optimizer.step()
            step += 1

        summary_writer.add_scalar('Metrics/loss_impacting_samples', loss_impacting_samples_count / samples_count * 100,
                                  global_step=epoch_id)

        if epoch_id % FLAGS.validation_freq == 0:
            model.eval()
            _, _, hits_at_10, _ = test(model=model, data_generator=validation_generator,
                                       entities_count=len(entity2id),
                                       device=device, summary_writer=summary_writer,
                                       epoch_id=epoch_id, metric_suffix="val")
            score = hits_at_10
            if score > best_score:
                best_score = score
                storage.save_checkpoint(model, optimizer, epoch_id, step, best_score)

    # Testing the best checkpoint on test dataset
    storage.load_checkpoint("checkpoint.tar", model, optimizer)
    best_model = model.to(device)
    best_model.eval()
    scores = test(model=best_model, data_generator=test_generator, entities_count=len(entity2id), device=device,
                  summary_writer=summary_writer, epoch_id=1, metric_suffix="test")
    print("Test scores: ", scores)


if __name__ == '__main__':
    app.run(main)
