"""Vectores de la figura 8.1 y reglas de la sección 8.2."""
import pytest

from archilector.enlace import protocolo as P


@pytest.mark.parametrize(
    "seq,cuerpo,esperada",
    [
        (17, "TURN", "$17,TURN*37"),
        (17, "ACK", "$17,ACK*63"),
        (17, "DONE,4820", "$17,DONE,4820*08"),
    ],
)
def test_vectores_del_documento(seq, cuerpo, esperada):
    cmd, *args = cuerpo.split(",")
    assert P.armar(seq, cmd, *args) == esperada + "\n"
    t = P.analizar(esperada)
    assert (t.seq, t.cmd, t.args) == (seq, cmd, args)


def test_checksum_incorrecto_es_E01():
    with pytest.raises(P.ErrorTrama) as e:
        P.analizar("$17,TURN*00")
    assert e.value.codigo == "E01"


def test_descarta_lo_que_no_empieza_con_dolar_o_es_largo():
    assert P.analizar("rst:0x1 (POWERON_RESET),boot:0x13") is None
    assert P.analizar("$" + "A" * 70) is None


def test_ultimo_argumento_conserva_comas():
    # Como en el firmware: hasta 3 argumentos y el tercero se queda con el resto
    t = P.analizar(P.armar(5, "LCD", 2, "PAG 1,2"))
    assert t.args == ["2", "PAG 1", "2"]
    t = P.analizar(P.armar(0, "EVT", "STATE", "ARRANQUE", "E07,X"))
    assert t.args == ["STATE", "ARRANQUE", "E07,X"]


def test_trama_demasiado_larga_no_se_arma():
    with pytest.raises(ValueError):
        P.armar(1, "LCD", 1, "X" * 60)


def test_texto_lcd():
    assert P.texto_lcd("Página 23 leída") == "PAGINA 23 LEIDA"
    assert len(P.texto_lcd("x" * 40)) == 16
