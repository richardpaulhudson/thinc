from typing import Tuple, Callable, Optional, TypeVar, Union, cast, List

from ..types import Padded, Ragged, Floats3d, Ints1d, List2d, Array2d
from ..model import Model
from ..config import registry
from ..util import is_xp_array


PaddedData = Tuple[Floats3d, Ints1d, Ints1d, Ints1d]
SeqT = TypeVar("SeqT", bound=Union[Padded, Ragged, List2d, Floats3d, PaddedData])


@registry.layers("with_padded.v1")
def with_padded(layer: Model[Padded, Padded]) -> Model[SeqT, SeqT]:
    return Model(
        f"with_padded({layer.name})",
        forward,
        init=init,
        layers=[layer],
        dims={name: layer.maybe_get_dim(name) for name in layer.dim_names},
    )


def forward(
    model: Model[SeqT, SeqT], Xseq: SeqT, is_train: bool
) -> Tuple[SeqT, Callable]:
    layer: Model[Padded, Padded] = model.layers[0]
    if isinstance(Xseq, Padded):
        padded_Y, backprop = layer(Xseq, is_train)
        Y = cast(SeqT, padded_Y)
    elif isinstance(Xseq, Ragged):
        ragged_Y, backprop = _ragged_forward(layer, cast(Ragged, Xseq), is_train)
        Y = cast(SeqT, ragged_Y)
    elif _is_padded_data(Xseq):
        padded_data_Y, backprop = _tuple_forward(
            layer, cast(PaddedData, Xseq), is_train
        )
        Y = cast(SeqT, padded_data_Y)
    elif is_xp_array(Xseq):
        floats3d_Y, backprop = _array_forward(layer, cast(Floats3d, Xseq), is_train)
        Y = cast(SeqT, floats3d_Y)
    else:
        list_Y, backprop = _list_forward(layer, cast(List2d, Xseq), is_train)
        Y = cast(SeqT, list_Y)
    return Y, backprop


def init(
    model: Model[SeqT, SeqT], X: Optional[SeqT] = None, Y: Optional[SeqT] = None
) -> Model[SeqT, SeqT]:
    model.layers[0].initialize(
        X=_get_padded(model, X) if X is not None else None,
        Y=_get_padded(model, Y) if Y is not None else None,
    )
    return model


def _is_padded_data(seq: SeqT) -> bool:
    return isinstance(seq, tuple) and len(seq) == 4 and all(map(is_xp_array, seq))


def _get_padded(model: Model, seq: SeqT) -> Padded:
    if isinstance(seq, Padded):
        return seq
    elif isinstance(seq, Ragged):
        return model.ops.list2padded(
            cast(List2d, model.ops.unflatten(seq.data, seq.lengths))
        )
    elif _is_padded_data(seq):
        return Padded(*seq)  # type: ignore[misc]
    elif is_xp_array(seq):
        floats3d_seq = cast(Floats3d, seq)
        size_at_t = model.ops.asarray1i([floats3d_seq.shape[1]] * floats3d_seq.shape[0])
        lengths = model.ops.asarray1i([floats3d_seq.shape[0]] * floats3d_seq.shape[1])
        indices = model.ops.xp.arange(floats3d_seq.shape[1])
        return Padded(floats3d_seq, size_at_t, lengths, indices)
    else:
        assert isinstance(seq, list), seq
        return model.ops.list2padded(seq)


def _array_forward(
    layer: Model[Padded, Padded], X: Floats3d, is_train
) -> Tuple[Floats3d, Callable]:
    # Create bogus metadata for Padded.
    Xp = _get_padded(layer, X)
    Yp, get_dXp = layer(Xp, is_train)
    size_at_t = Xp.size_at_t
    lengths = Xp.lengths
    indices = Xp.indices

    def backprop(dY: Floats3d) -> Floats3d:
        dYp = Padded(dY, size_at_t, lengths, indices)
        dXp = get_dXp(dYp)
        return dXp.data

    return cast(Floats3d, Yp.data), backprop


def _tuple_forward(
    layer: Model[Padded, Padded], X: PaddedData, is_train: bool
) -> Tuple[PaddedData, Callable]:
    Yp, get_dXp = layer(Padded(*X), is_train)

    def backprop(dY):
        dXp = get_dXp(Padded(*dY))
        return (dXp.data, dXp.size_at_t, dXp.lengths, dXp.indices)

    return (cast(Floats3d, Yp.data), Yp.size_at_t, Yp.lengths, Yp.indices), backprop


def _ragged_forward(
    layer: Model[Padded, Padded], Xr: Ragged, is_train: bool
) -> Tuple[Ragged, Callable]:
    # Assign these to locals, to keep code a bit shorter.
    list2padded = layer.ops.list2padded
    padded2list = layer.ops.padded2list
    unflatten = layer.ops.unflatten
    flatten = layer.ops.flatten
    # It's worth being a bit careful about memory here, as the activations
    # are potentially large on GPU. So we make nested function calls instead
    # of assigning to temporaries where possible, so memory can be reclaimed
    # sooner.
    Yp, get_dXp = layer(
        list2padded(cast(List2d, unflatten(Xr.data, Xr.lengths))), is_train
    )

    def backprop(dYr: Ragged):
        flattened = flatten(
            cast(
                List[Array2d],
                padded2list(
                    get_dXp(list2padded(cast(List2d, unflatten(dYr.data, dYr.lengths))))
                ),
            )
        )
        return Ragged(flattened, dYr.lengths)

    flattened = flatten(cast(List[Array2d], padded2list(Yp)))
    return Ragged(flattened, Xr.lengths), backprop


def _list_forward(
    layer: Model[Padded, Padded], Xs: List2d, is_train: bool
) -> Tuple[List2d, Callable]:
    # Assign these to locals, to keep code a bit shorter.
    list2padded = layer.ops.list2padded
    padded2list = layer.ops.padded2list

    Yp, get_dXp = layer(list2padded(Xs), is_train)

    def backprop(dYs):
        return padded2list(get_dXp(list2padded(dYs)))

    return padded2list(Yp), backprop
