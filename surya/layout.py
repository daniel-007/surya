from typing import List

import numpy as np
import torch
from PIL import Image

from tqdm import tqdm

from surya.model.layout.config import ID_TO_LABEL
from surya.schema import LayoutResult, LayoutBox
from surya.settings import settings


def get_batch_size():
    batch_size = settings.LAYOUT_BATCH_SIZE
    if batch_size is None:
        batch_size = 4
        if settings.TORCH_DEVICE_MODEL == "mps":
            batch_size = 4
        if settings.TORCH_DEVICE_MODEL == "cuda":
            batch_size = 32
    return batch_size


def prediction_to_polygon(pred, img_size, bbox_scaler, skew_scaler):
    w_scale = img_size[0] / bbox_scaler
    h_scale = img_size[1] / bbox_scaler

    boxes = pred #* bbox_scaler
    cx = boxes[:, 0]
    cy = boxes[:, 1]
    width = boxes[:, 2]
    height = boxes[:, 3]
    x1 = cx - width / 2
    y1 = cy - height / 2
    x2 = cx + width / 2
    y2 = cy + height / 2
    skew_x = (boxes[:, 4] - skew_scaler) / 2
    skew_y = (boxes[:, 5] - skew_scaler) / 2
    polygon = torch.stack(
        [x1 - skew_x, y1 - skew_y, x2 - skew_x, y1 + skew_y, x2 + skew_x, y2 + skew_y, x1 + skew_x, y2 - skew_y],
        dim=-1)
    polygon = polygon * torch.tensor([w_scale, h_scale, w_scale, h_scale, w_scale, h_scale, w_scale, h_scale],
                                      dtype=polygon.dtype, device=polygon.device)
    return polygon


def batch_layout_detection(images: List, model, processor, batch_size=None) -> List[LayoutResult]:
    assert all([isinstance(image, Image.Image) for image in images])
    if batch_size is None:
        batch_size = get_batch_size()

    results = []
    for i in tqdm(range(0, len(images), batch_size), desc="Recognizing layout"):
        batch_images = images[i:i+batch_size]
        batch_images = [image.convert("RGB") for image in batch_images]  # also copies the image
        current_batch_size = len(batch_images)

        orig_sizes = [image.size for image in batch_images]
        model_inputs = processor(batch_images)

        batch_pixel_values = model_inputs["pixel_values"]
        batch_pixel_values = torch.tensor(np.array(batch_pixel_values), dtype=model.dtype).to(model.device)

        batch_decoder_input = [
        [
            [model.config.decoder.bos_token_id] * 7,
            [orig_sizes[j][1] // model.config.decoder.img_size_bucket, orig_sizes[j][0] // model.config.decoder.img_size_bucket] + [model.config.decoder.size_token_id] * 5

        ] for j in range(current_batch_size)]
        batch_decoder_input = torch.tensor(np.stack(batch_decoder_input, axis=0), dtype=torch.long, device=model.device)
        inference_token_count = batch_decoder_input.shape[1]

        decoder_position_ids = torch.ones_like(batch_decoder_input[0, :, 0], dtype=torch.int64, device=model.device).cumsum(0) - 1
        model.decoder.model._setup_cache(model.config, batch_size, model.device, model.dtype)
        model.text_encoder.model._setup_cache(model.config, batch_size, model.device, model.dtype)

        batch_predictions = [[] for _ in range(len(images))]

        with torch.inference_mode():
            encoder_hidden_states = model.encoder(pixel_values=batch_pixel_values)[0]

            text_encoder_input_ids = torch.arange(
                model.text_encoder.config.query_token_count,
                device=model.device,
                dtype=torch.long
            ).unsqueeze(0).expand(encoder_hidden_states.size(0), -1)

            # Encode text
            text_encoder_outputs = model.text_encoder(
                input_ids=text_encoder_input_ids,
                inputs_embeds=None,
                cache_position=None,
                attention_mask=None,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=None,
                use_cache=False
            ).hidden_states
            del encoder_hidden_states

            token_count = 0
            all_done = torch.zeros(current_batch_size, dtype=torch.bool, device=model.device)

            while token_count < settings.LAYOUT_MAX_BOXES:
                is_prefill = token_count == 0
                return_dict = model.decoder(
                    input_boxes=batch_decoder_input,
                    encoder_hidden_states=text_encoder_outputs,
                    cache_position=decoder_position_ids,
                    use_cache=True,
                    prefill=is_prefill
                )

                decoder_position_ids = decoder_position_ids[-1:] + 1
                box_logits = return_dict["bbox_logits"][:current_batch_size, -1, :].detach()
                class_logits = return_dict["class_logits"][:current_batch_size, -1, :].detach()

                class_preds = class_logits.argmax(-1)
                box_preds = box_logits.argmax(-1)

                done = (class_preds == model.decoder.config.eos_token_id) | (class_preds == model.decoder.config.pad_token_id)

                all_done = all_done | done

                if all_done.all():
                    break

                batch_decoder_input = torch.cat([box_preds.unsqueeze(1), class_preds.unsqueeze(1).unsqueeze(1)], dim=-1)

                for j, (pred, status) in enumerate(zip(batch_decoder_input, all_done)):
                    if not status:
                        batch_predictions[j].append(pred[0])

                token_count += inference_token_count
                inference_token_count = batch_decoder_input.shape[1]

        for j, (preds, orig_size) in enumerate(zip(batch_predictions, orig_sizes)):
            stacked_preds = torch.stack(preds, dim=0)
            polygons = prediction_to_polygon(stacked_preds, orig_size, model.config.decoder.bbox_size, model.config.decoder.skew_scaler)
            labels = stacked_preds[:, 6] - model.decoder.config.special_token_count

            boxes = []
            for z, (polygon, label) in enumerate(zip(polygons, labels)):
                poly = polygon.tolist()
                poly = [
                    (poly[2 * i], poly[2 * i + 1])
                    for i in range(4)
                ]
                lb = LayoutBox(
                    polygon=poly,
                    label=ID_TO_LABEL[label.item()],
                    position=z
                )
                boxes.append(lb)
            result = LayoutResult(
                bboxes=boxes,
                image_bbox=[0, 0, orig_size[0], orig_size[1]]
            )
            results.append(result)
    return results
