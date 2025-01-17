import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.utils import to_dense_batch

from model_Yang import GNN
from resnet import ResnetEncoderModel


class DTI_model(nn.Module):
    def __init__(
        self,
        all_config=None,
        contextpred_config={
            "num_layer": 5,
            "emb_dim": 300,
            "JK": "last",
            "drop_ratio": 0.5,
            "gnn_type": "gin",
        },
        model=None,
    ):
        super(DTI_model, self).__init__()
        # -------------------------------------------
        #         hyper-parameter
        # -------------------------------------------
        self.use_cuda = all_config["use_cuda"]
        self.contextpred_config = contextpred_config
        self.all_config = all_config
        # self.tape_related = tape_related
        # -------------------------------------------
        #         model components
        # -------------------------------------------
        #        interaction
        # self.attentive_interaction_pooler = AttentivePooling2(contextpred_config['emb_dim'])
        # self.binary_predictor = EmbeddingTransform2(contextpred_config['emb_dim']*2, hidden_size=256,out_size=2)
        #          chemical decriptor
        self.ligandEmbedding = GNN(
            num_layer=contextpred_config["num_layer"],
            emb_dim=contextpred_config["emb_dim"],
            JK=contextpred_config["JK"],
            drop_ratio=contextpred_config["drop_ratio"],
            gnn_type=contextpred_config["gnn_type"],
        )

        #          protein decriptor
        proteinEmbedding = model
        self.proteinEmbedding = proteinEmbedding
        if all_config["protein_descriptor"] == "DISAE":
            prot_embed_dim = 256
        elif all_config["protein_descriptor"] == "TAPE":
            prot_embed_dim = 768
        else:
            prot_embed_dim = 1280
        if all_config["frozen"] == "partial":
            prot_embed_dim = 256
            ct = 0
            for m in self.proteinEmbedding.modules():
                ct += 1
                if ct in all_config["DISAE"]["frozen_list"]:
                    # print('frozen module ', ct)
                    for param in m.parameters():
                        param.requires_grad = False
                else:
                    for param in m.parameters():
                        param.requires_grad = True
                # else:
        self.resnet = ResnetEncoderModel(1)
        # print('plus Resnet!')
        # self.prot_embed_transform = nn.Linear(prot_embed_dim,contextpred_config['emb_dim'])
        #        interaction
        self.attentive_interaction_pooler = AttentivePooling(
            contextpred_config["emb_dim"],
        )
        self.interaction_pooler = EmbeddingTransform(
            contextpred_config["emb_dim"] + prot_embed_dim, 128, 64, 0.1
        )
        self.binary_predictor = EmbeddingTransform(64, 64, 2, 0.2)

        if self.use_cuda and torch.cuda.is_available():
            self.attentive_interaction_pooler = self.attentive_interaction_pooler.to(
                "cuda"
            )
            self.interaction_pooler = self.interaction_pooler.to("cuda")
            self.binary_predictor = self.binary_predictor.to("cuda")
            self.ligandEmbedding = self.ligandEmbedding.to("cuda")
            self.proteinEmbedding = self.proteinEmbedding.to("cuda")

            # self.prot_embed_transform = self.prot_embed_transform.to('cuda')

    def forward(self, batch_protein_tokenized, batch_chem_graphs, **kwargs):
        # ---------------protein embedding ready -------------
        if self.all_config["protein_descriptor"] == "DISAE":
            if self.all_config["frozen"] == "whole":
                with torch.no_grad():
                    batch_protein_repr = self.proteinEmbedding(batch_protein_tokenized)[
                        0
                    ]
            else:
                batch_protein_repr = self.proteinEmbedding(batch_protein_tokenized)[0]

            batch_protein_repr_resnet = self.resnet(
                batch_protein_repr.unsqueeze(1)
            ).reshape(
                self.all_config["batch_size"], 1, -1
            )  # (batch_size,1,256)

        # ---------------ligand embedding ready -------------
        node_representation = self.ligandEmbedding(
            batch_chem_graphs.x,
            batch_chem_graphs.edge_index,
            batch_chem_graphs.edge_attr,
        )
        batch_chem_graphs_repr_masked, mask_graph = to_dense_batch(
            node_representation, batch_chem_graphs.batch
        )
        batch_chem_graphs_repr_pooled = batch_chem_graphs_repr_masked.sum(
            axis=1
        ).unsqueeze(
            1
        )  # (batch_size,1,300)
        # ---------------interaction embedding ready -------------
        ((chem_vector, chem_score), (prot_vector, prot_score)) = (
            self.attentive_interaction_pooler(
                batch_chem_graphs_repr_pooled, batch_protein_repr_resnet
            )
        )  # same as input dimension

        # interaction_vector = torch.cat((prot_vec, chem_vec), dim=1)
        interaction_vector = self.interaction_pooler(
            torch.cat((chem_vector.squeeze(), prot_vector.squeeze()), 1)
        )  # (batch_size,64)
        logits = self.binary_predictor(interaction_vector)  # (batch_size,2)
        return logits


class EmbeddingTransform2(nn.Module):
    def __init__(self, input_size, hidden_size, out_size, dropout_p=0.1):
        super(EmbeddingTransform2, self).__init__()
        self.dropout = nn.Dropout(p=dropout_p)
        self.transform = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.BatchNorm1d(hidden_size),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_size, out_size),
            nn.BatchNorm1d(out_size),
        )

    def forward(self, embedding):
        embedding = self.dropout(embedding)
        hidden = self.transform(embedding)
        return hidden


class EmbeddingTransform(nn.Module):
    def __init__(self, input_size, hidden_size, out_size, dropout_p=0.1):
        super(EmbeddingTransform, self).__init__()
        self.dropout = nn.Dropout(p=dropout_p)
        self.transform = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.BatchNorm1d(hidden_size),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_size, out_size),
            nn.BatchNorm1d(out_size),
        )

    def forward(self, embedding):
        embedding = self.dropout(embedding)
        hidden = self.transform(embedding)
        return hidden


class AttentivePooling2(nn.Module):
    """Attentive pooling network according to https://arxiv.org/pdf/1602.03609.pdf"""

    def __init__(self, embedding_length=300):
        super(AttentivePooling2, self).__init__()
        self.embedding_length = embedding_length
        self.U = nn.Parameter(torch.zeros(self.embedding_length, self.embedding_length))

    def forward(self, protein, ligand):
        """Calculate attentive pooling attention weighted representation and"""

        U = self.U.expand(protein.size(0), self.embedding_length, self.embedding_length)
        Q = protein
        A = ligand
        G = torch.tanh(torch.bmm(torch.bmm(Q, U), A.transpose(1, 2)))
        g_q = G.max(axis=2).values
        g_a = G.max(axis=1).values

        def get_attention_score(g_q, Q):
            g_q_masked = g_q.masked_fill(g_q == 0, -1e9)
            sigma_q = F.softmax(g_q_masked)
            prot_repr = Q * sigma_q[:, :, None]
            prot_vec = prot_repr.sum(1)
            return sigma_q, prot_vec

        sigma_q, prot_vec = get_attention_score(g_q, Q)
        sigma_a, chem_vec = get_attention_score(g_a, A)

        return sigma_q, prot_vec, sigma_a, chem_vec


class AttentivePooling(nn.Module):
    """Attentive pooling network according to https://arxiv.org/pdf/1602.03609.pdf"""

    def __init__(self, chem_hidden_size=128, prot_hidden_size=256):
        super(AttentivePooling, self).__init__()
        self.chem_hidden_size = chem_hidden_size
        self.prot_hidden_size = prot_hidden_size
        self.param = nn.Parameter(torch.zeros(chem_hidden_size, prot_hidden_size))

    def forward(self, first, second):
        """Calculate attentive pooling attention weighted representation and
        attention scores for the two inputs.

        Args:
            first: output from one source with size (batch_size, length_1, hidden_size)
            second: outputs from other sources with size (batch_size, length_2, hidden_size)

        Returns:
            (rep_1, attn_1): attention weighted representations and attention scores
            for the first input
            (rep_2, attn_2): attention weighted representations and attention scores
            for the second input
        """
        # logging.debug("AttentivePooling first {0}, second {1}".format(first.size(), second.size()))
        param = self.param.expand(
            first.size(0), self.chem_hidden_size, self.prot_hidden_size
        )
        # logging.debug("AttentivePooling params: {0}".format(param.size()))
        wm1 = torch.tanh(torch.bmm(second, param.transpose(1, 2)))
        wm2 = torch.tanh(torch.bmm(first, param))
        # logging.debug("Wm1 {}, Wm2 {} before softmax".format(wm1.size(),wm2.size()))
        score_m1 = F.softmax(wm1, dim=2)
        score_m2 = F.softmax(wm2, dim=2)
        # logging.debug("score_m1 {}, score_m2 {}".format(score_m1.size(),score_m2.size()))
        rep_first = first * score_m1
        rep_second = second * score_m2
        # logging.debug("AttentivePooling reps: {0}, {1}".format(rep_first.size(), rep_second.size()))

        return ((rep_first, score_m1), (rep_second, score_m2))
