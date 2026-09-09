# Might need to review and make sure everyone is here
PARTIES = [
    " AP ",
    " AP-PIS ",
    " APP ",
    " BM ",
    " BDP ",
    " BOP ",
    " BS ",
    " BSS ",
    " E ",
    " FP ",
    " HYD ",
    " JP ",
    " IJPP-VP ",
    " JPP-VP ",
    " NA ",
    " NP ",
    " PL ",
    " PLG ",
    " PM ",
    " PP ",
    " SP ",
    " sP ",
    " RP ",
    " 8S ",
    " 8M ",
]

# Dictionary to avoid creation of duplicate parties objects
PARTY_ALIASES = {
    "Alianza para el Progreso": "Alianza para el Progreso del Perú",
    "Somos Perú": "Partido Democrático Somos Perú",
    "Frente Amplio": "Frente Amplio por Justicia, Vida y Libertad",
    "Frente Popular Agrícola del Perú": "Frente Popular Agrícola FIA del Perú",
    "No Agrupado": "Ninguno",
    "No ha acreditado": "Ninguno",
    "No registrado": "Ninguno",
    "Alianza Solidaridad Nacional": "Solidaridad Nacional",
    "Unión por el Perú": "Unión por el Perú - Social Democracia",
}

LEG_PERIOD_ALIASES = {
    "Parlamentario 2026 - 2031": "2026-2031",
    "Parlamentario 2021 - 2026": "2021-2026",
    "Parlamentario 2021-2026": "2021-2026",
    "2021 - 2026": "2021-2026",
    "2021–2026": "2021-2026",
    "2021-2026": "2021-2026",
    "Parlamentario 2016 - 2021": "2016-2021",
    "2016 - 2021": "2016-2021",
    "2016–2021": "2016-2021",
    "2016-2021": "2016-2021",
    "Parlamentario 2011 - 2016": "2011-2016",
    "2011 - 2016": "2011-2016",
    "2011–2016": "2011-2016",
    "2011-2016": "2011-2016",
    "Parlamentario 2006 - 2011": "2006-2011",
    "2006 - 2011": "2006-2011",
    "2006–2011": "2006-2011",
    "2006-2011": "2006-2011",
    "Parlamentario 2001 - 2006": "2001-2006",
    "2001 - 2006": "2001-2006",
    "2001–2006": "2001-2006",
    "2001-2006": "2001-2006",
    "Parlamentario 2000 - 2001": "2000-2001",
    "2000 - 2001": "2000-2001",
    "2000–2001": "2000-2001",
    "2000-2001": "2000-2001",
    "Parlamentario 1995 - 2000": "1995-2000",
    "1995 - 2000": "1995-2000",
    "1995–2000": "1995-2000",
    "1995-2000": "1995-2000",
    "CCD 1992 -1995": "1992-1995",
    "CCD 1992 - 1995": "1992-1995",
    "1992-1995": "1992-1995",
    "Parlamentario 1995-2000": "1995-2000",
    "CCD 1992-1995": "1992-1995",
}


# Single source of truth for "which legislative periods are we willing to
# process" — previously expressed independently as separate hardcoded
# allowlists in _process_congresistas/_process_bancada_definitions/
# _process_bancada_memberships plus a separate year-range in
# _process_organization_definitions, which risked drifting out of sync.
PROCESSABLE_LEG_PERIODS = [
    "Parlamentario 2021 - 2026",
    "Parlamentario 2016 - 2021",
    "Parlamentario 2026 - 2031",
]

# The whole-Congress body: parent for both legacy (2021-2026, unicameral)
# entities and 2026-2031 joint/bicameral entities (e.g. "Comisión Permanente",
# "Comisión Bicameral de Presupuesto y Cuenta General de la República") --
# confirmed 2026-09-08 as a real, distinct top-level Organization (not NULL
# parent, not "Cámara de Diputados") kept deliberately separate so
# 2021-2026-term data never gets conflated with the 2026-2031 bicameral
# chambers.
WHOLE_CONGRESS_ORG_NAME = "Congreso de la República"

# Maps a raw scraped chamber label (RawCongresista.chamber / RawBancada.chamber /
# RawCommittee.chamber / RawOrganization.chamber) to the canonical parent
# Organization name. Two distinct "no specific chamber" cases, both resolving
# to WHOLE_CONGRESS_ORG_NAME:
#   - None: not specified / legacy pre-2026 data. This constant previously
#     mapped None to "Cámara de Diputados", which was never correct for this
#     data (every pre-existing 2021-2026 standing/special committee in
#     production is parented under WHOLE_CONGRESS_ORG_NAME) and caused every
#     legacy committee lookup scoped by that default to silently miss
#     ("organization not found").
#   - "Congreso": a CONFIRMED joint/bicameral entity for the 2026-2031 term.
#     Previously mapped to None (no parent at all); unified with the legacy
#     case above so every joint/whole-Congress body -- legacy or bicameral --
#     is a child of one real, stable parent instead of a mix of NULL-parent
#     orphans and legacy-parented rows.
# Any other value raises (KeyError), by design — see backend/database/orchestrator.py
# per-row exception handling, which catches and counts this in stats.errors rather
# than silently misattributing an unrecognized label to the wrong chamber.
CHAMBER_LABEL_TO_ORG_NAME = {
    "Diputados": "Cámara de Diputados",
    "Senadores": "Senado de la República",
    "Congreso": WHOLE_CONGRESS_ORG_NAME,
    None: WHOLE_CONGRESS_ORG_NAME,
}

# Per-chamber site roots for the 2026-2031 term (confirmed live 2026-08-31).
# These are separate WordPress microsites, not a chamber query param on the
# legacy www3.congreso.gob.pe site.
CHAMBER_BASE_URLS = {
    "Senadores": "https://senado.congreso.gob.pe",
    "Diputados": "https://diputados.congreso.gob.pe",
}

# The one raw leg_period label used for every 2026-2031 chamber-scraped row
# (RawCongresista.leg_period, RawBancada.legislative_period) -- matches
# LEG_PERIOD_ALIASES/PROCESSABLE_LEG_PERIODS, which already expect this exact
# string, so no changes needed there.
CHAMBER_LEG_PERIOD_LABEL = "Parlamentario 2026 - 2031"

# Bills/motions bicameral scraping chamber-derived maps. Distinct from
# CHAMBER_LABEL_TO_ORG_NAME above (that maps to canonical Organization
# names for the process layer) and from chamber_label_from_id's suffix map
# in backend/process/utils.py (that maps id-suffix -> label, the inverse
# direction) -- these three map a resolved chamber label to the specific
# wire formats congreso.gob.pe's bills/motions APIs and the bills SPA expect.
CHAMBER_LABEL_TO_COD_TIPO_PARL = {"Senadores": "S", "Diputados": "D"}  # API wire code

CHAMBER_LABEL_TO_ID_SUFFIX = {
    "Senadores": "S",
    "Diputados": "CD",
}  # RawBill/RawMotion id suffix

# perParId for the 2026-2031 period -- also doubles as motions' detail-URL
# "year" path segment (confirmed live: GET .../mocion/{S|D}/2026/{number}
# uses the bare period-start year, not the full period string). Legacy
# get_last_id()/_scrape_range() keep their own separate hardcoded 2021
# literals unchanged (by design, to guarantee zero legacy behavior change),
# so this deliberately has no "2021-2026" entry -- a dict entry nothing
# reads would be an unenforced claim that this is a single source of truth
# when it demonstrably isn't wired up as one.
LEG_PERIOD_TO_PER_PAR_ID = {"2026-2031": 2026}

# The bills SPA's hash-route chamber prefix (e.g.
# {BASE_URL}/senado/expediente/2026-2031/{number}), confirmed live.
CHAMBER_LABEL_TO_ROUTE_SLUG = {"Senadores": "senado", "Diputados": "diputados"}

LEGISLATURE_ALIASES = {
    # Congress wording → canonical legislature code
    # 2030
    "Primera Legislatura Ordinaria 2030": "2030-II",
    "Segunda Legislatura Ordinaria 2030": "2031-I",
    # 2029
    "Primera Legislatura Ordinaria 2029": "2029-II",
    "Segunda Legislatura Ordinaria 2029": "2030-I",
    # 2028
    "Primera Legislatura Ordinaria 2028": "2028-II",
    "Segunda Legislatura Ordinaria 2028": "2029-I",
    # 2027
    "Primera Legislatura Ordinaria 2027": "2027-II",
    "Segunda Legislatura Ordinaria 2027": "2028-I",
    # 2026
    "Primera Legislatura Ordinaria 2026": "2026-II",
    "Segunda Legislatura Ordinaria 2026": "2027-I",
    # 2025
    "Primera Legislatura Ordinaria 2025": "2025-II",
    "Segunda Legislatura Ordinaria 2025": "2026-I",
    # 2024
    "Primera Legislatura Ordinaria 2024": "2024-II",
    "Segunda Legislatura Ordinaria 2024": "2025-I",
    # 2023
    "Primera Legislatura Ordinaria 2023": "2023-II",
    "Segunda Legislatura Ordinaria 2023": "2024-I",
    # 2022
    "Primera Legislatura Ordinaria 2022": "2022-II",
    "Segunda Legislatura Ordinaria 2022": "2023-I",
    # 2021
    "Primera Legislatura Ordinaria 2021": "2021-II",
    "Segunda Legislatura Ordinaria 2021": "2022-I",
    # 2020
    "Primera Legislatura Ordinaria 2020": "2020-II",
    "Segunda Legislatura Ordinaria 2020": "2021-I",
    # 2019
    "Primera Legislatura Ordinaria 2019": "2019-II",
    "Segunda Legislatura Ordinaria 2019": "2020-I",
    # 2018
    "Primera Legislatura Ordinaria 2018": "2018-II",
    "Segunda Legislatura Ordinaria 2018": "2019-I",
    # 2017
    "Primera Legislatura Ordinaria 2017": "2017-II",
    "Segunda Legislatura Ordinaria 2017": "2018-I",
}


BILL_ROLE_MAPS = {1: "author", 2: "coauthor", 3: "adherente"}

LEGAL_TERMS = {
    r"\bdecreto\s+legislativo\b": "Decreto Legislativo",
    r"\bdecreto\s+supremo\b": "Decreto Supremo",
    r"\bdecreto\s+de\s+urgencia\b": "Decreto de Urgencia",
    r"\bresoluci[oó]n\s+ministerial\b": "Resolución Ministerial",
    r"\bresoluci[oó]n\s+legislativa\b": "Resolución Legislativa",
    r"\bley\b": "Ley",
    r"\bproyecto\s+de\s+ley\b": "Proyecto de Ley",
}

REGIONS_MAP = {
    "Loreto": "Loreto",
    "Lambayeque": "Lambayeque",
    "Cusco": "Cusco",
    "Piura": "Piura",
    "La libertad": "La Libertad",
    "Amazonas": "Amazonas",
    "Puno": "Puno",
    "Pasco": "Pasco",
    "Ancash": "Áncash",
    "Huanuco": "Huánuco",
    "San martin": "San Martín",
    "Ica": "Ica",
    "Ucayali": "Ucayali",
    "Tumbes": "Tumbes",
    "Junin": "Junín",
    "Ayacucho": "Ayacucho",
    "Tacna": "Tacna",
    "Callao": "Callao",
    "Madre de dios": "Madre de Dios",
    "LIMA": "Lima",
    "Moquegua": "Moquegua",
    "Arequipa": "Arequipa",
    "Peruanos Residentes en el Extranjero": "Peruanos Residentes en el Extranjero",
    "Lima Provincias": "Lima Provincias",
    "Apurimac": "Apurímac",
    "Cajamarca": "Cajamarca",
    "Huancavelica": "Huancavelica",
}

COMISION_SHORT_NAMES = {
    "Crear una Comisión Investigadora, de conformidad con el artículo 97º de la Constitución Política del Perú, concordante con el artículo 88º del Reglamento del Congreso de la República, para que en el plazo de 45 días calendarios, se encargue de investigar las presuntas irregularidades en el control, diagnóstico y tratamiento de la pandemia ocasionada por el Covid-19, así como también de las adquisiciones realizadas en dicho marco, por parte de los Gobierno Regionales de Tumbes, Ucayali y Apurímac. (Moción 14312)": "Comisión COVID-19 Tumbes, Ucayali y Apurímac",
    "Comisión Especial Encargada del Seguimiento y Formular Propuestas para la Mitigación y Adaptación del Cambio Climático por el Periodo Parlamentario 2016-2021.": "Comisión Cambio Climático",
    "Comisión Investigadora especial multipartidaria encargada de evaluar, proponer, fiscalizar e impulsar la tercera etapa del Proyecto Especial Chavimochic en la región La Libertad, por el plazo de 120 días (Moción de Orden del día 918)": "Comisión Chavimochic",
    "Comisión Investigadora encargada de investigar las presuntas irregularidades y posibles actos de corrupción en la gestión de las contrataciones y adquisiciones de bienes y servicios realizadas por el Seguro Social de Salud – ESSALUD, MINSA, gobiernos locales y gobiernos regionales durante el período de emergencia sanitaria nacional por motivo del Covid-19, desde marzo de 2020 hasta la actualidad, por un plazo de 180 días hábiles. (Moción del Orden del Día 007)": "Comisión Compras COVID-19",
    "Comisión de Levantamiento de Inmunidad Parlamentaria": "Comisión de Inmunidad Parlamentaria",
    "Comisión Especial Multipartidaria de Protección a la Infancia en el contexto de la emergencia sanitaria, encargada de realizar labores de monitoreo de  políticas públicas, programas y servicios; coordine con las comisiones multisectoriales; colabore en el desarrollo de propuestas normativas; y fiscalice  en los tres niveles de gobierno, en torno a las problemáticas de la infancia, las que se han agudizado en la actual crisis sanitaria; tendrá una duración  de dos primeras legislaturas ordinarias del actual periodo parlamentario. (Moción de Orden del día 76)": "Comisión de Protección a la Infancia",
    "Comisión multipartidaria de análisis, seguimiento, coordinación y formulación de propuestas para el Proyecto Binacional Puyango-Tumbes, durante el  periodo parlamentario 2021-2026, con la finalidad de contribuir con los esfuerzos en materia legislativa, de seguimiento y control político a las acciones  desarrolladas por el Poder Ejecutivo, el Gobierno Regional de Tumbes y el Congreso de la República (Moción de orden del día 345)": "Comisión Puyango-Tumbes",
    "Comisión especial multipartidaria de impulso y seguimiento del proyecto Terminal Multipropósito de Chancay, para realizar el estudio, monitoreo y  seguimiento del proceso de implementación de la infraestructura portuaria y las obras complementarias, así como, para coordinar, promover y  recomendar las acciones de políticas públicas, planes, programas, estrategias de desarrollo productivo e industrial vinculados a la cadena comercial y  logística del transporte marítimo; igualmente, para promover las exportaciones con valor agregado a partir del terminal multipropósito de Chancay  (Moción de Orden del Día 8087)": "Comisión Terminal de Chancay",
    "Comisión especial para la elección del Defensor del Pueblo. Moción de Orden del día 865, 1191": "Comisión Defensor del Pueblo",
    "Comisión especial multipartidaria, enfocado en la implementación de la infraestructura tecnológica en las etapas del sistema educativo, de acuerdo a la Ley General de Educación, Ley 28044, que permita reducir las brechas en el sector educación flexibilizando la regulación  de normas en proyectos y soluciones innovadoras que genere mayor acceso a los servicios de comunicaciones en áreas rurales y de preferente interés social. La comisión estaría integrada por un congresista de cada grupo parlamentario y tendría  vigencia de un año, a partir de su instalación (Moción de Orden del Día 13129": "Comisión Brecha Digital Educativa",
    "Comisión investigadora Multipartidaria, que se encargará de investigar todos los actos vinculados a la negociación, celebración, homologación y ejecución del acuerdo de colaboración eficaz suscrito entre el Estado peruano y la empresa CONSTRUCTORA NORBERTO ODEBRECHT - SUCURSAL PERÚ y los efectos lesivos que dicho acuerdo haya generado en desmedro de los derechos e intereses del Estado peruano. (Moción de Orden del Día 15431": "Comisión Acuerdo con Odebrecht",
    "Comisión Especial Multipartidaria de Monitoreo, Fiscalización y Control del Programa Hambre Cero": "Comisión Hambre Cero",
    "Comisión Especial Multipartidaria a Favor de los Valles de los Ríos Apurímac, Ene y Mantaro para Estudiar, Monitorear, Evaluar, Proponer y Promover el Cumplimiento de las Políticas, Planes, Programas, Proyectos, Estrategias, entre otras acciones dadas por el Poder Ejecutivo a favor del VRAEM": "Comisión VRAEM",
    "Comisión especial multisectorial a favor de los Valles de los Ríos Apurímac, Ene y Mantaro – VRAEM, encargada de estudiar, monitorear, evaluar, proponer, promover y fiscalizar en los tres niveles de gobierno, el cumplimiento de las políticas, planes, programas, proyectos, estrategias, entre otras acciones dadas por el Poder Ejecutivo a favor del VRAEM, recomendando y tomando acciones conforme a sus funciones parlamentarias. (Moción de Orden del día 006)": "Comisión VRAEM",
    "Comisión Especial Multipartidaria, encargada del seguimiento y monitoreo a la eficiencia de la inversión pública alcanzada por los Gobiernos  Regionales en el Perú de acuerdo con lo establecido en el artículo 68° del Reglamento del Congreso de la República; Segundo: La presente Comisión  Especial tendría un plazo de 365 días calendario desde el momento de su instalación; Tercero: La presente Comisión Especial tendrá como finalidad  la realización del seguimiento y monitoreo de la eficiencia de la inversión pública alcanzada en los Gobierno Regionales, y canalizará ante las  instancias competentes la problemática en su gestión, así como formulará las recomendaciones correspondientes para minimizar los riesgos durante  todo el proceso de la inversión pública (Moción 8135)": "Comisión Inversión Pública Regional",
    "Comisión Especial Multisecotrial Revisora del Código de Ejecución Penal, creada por Ley 31588": "Comisión Código de Ejecución Penal",
    "Comisión Investigadora de las Presuntas Irregularidades y Posibles Actos de Corrupción en el Gobierno Regional del Callao": "Comisión Gobierno Regional del Callao",
    "Comisión especial multipartidaria, encargada de realizar trabajo en conjunto con la Comisión Nacional para el Desarrollo y Vida sin Drogas (DEVIDA) y  las entidades del Estado peruano responsables de los objetivos prioritarios y lineamientos en la lucha frontal contra el narcotráfico, en beneficio y  salvaguarda de las comunidades nativas, caseríos, centros poblados y concesionarios forestales de las regiones de Ucayali, Loreto, Amazonas, Pasco  y Madre de Dios, por un plazo de seis meses, pudiendo ampliarse por un plazo similar, conforme el literal c del artículo 35 del Reglamento del  Congreso (Moción de Orden del día 468)": "Comisión Lucha contra el Narcotráfico",
    "Comisión especial multipartidaria a favor de los Valles de los Ríos Apurímac, Ene y Mantaro – VRAEM, por el plazo que comprende el periodo  legislativo 2023 – 2026, para que estudie, monitoree, evalúe, proponga y promueva el cumplimiento de las políticas, planes, programas, proyectos,  estrategias, entre otras acciones dadas por el ejecutivo a favor del vraem, recomendando y tomando acciones conforme a sus funciones  parlamentarias. (Mociones de Orden del Día 7412, 7416, 8097": "Comisión VRAEM",
    "Comisión Investigadora por el plazo de 90 días calendarios para establecer el número real de fallecidos a causa del Covid-19, y como consecuencia de ello determinar las presuntas responsabilidades de los funcionarios y /o servidores públicos que hubieran estado involucrados en dichos actos, el mismo que coadyuvará a mejorar los procesos estatales de gestión, como parte de los deberes primordiales del Estado": "Comisión Fallecidos por COVID-19",
    "Comisión Especial Multipartidaria Encargada de Evaluar, Diseñar  y Proponer el Proyecto para la Reformar Integral del  Sistema Previsional Peruano": "Comisión Reforma del Sistema Previsional",
    "Comisión investigadora multipartidaria encargada determinar  las posibles responsabilidades política, penales y administrativas  a que  hubiera lugar, en torno a las muertes ocurridas durante las protestas ciudadanas iniciadas el 28 de marzo 2022, y contará con un plazo de novena (90) días hábiles para realizar la investigación y presentar su informe final. (Moción de Orden del Día 2348)": "Comisión Protestas de Marzo de 2022",
    "Comisión investigadora encargada de esclarecer el Contrato de Concesión de Reserva Fría de Generación del Proyecto Suministro de Energía para Iquitos —suscrito entre la empresa Genrent del Perú SAC, subsidiaria de Genrent de Brasil, y el Ministerio de Energía y Minas— y el Contrato de Suministro de Electricidad entre Genrent del Perú SAC y Electro Oriente SA, por el cual Genrent del Perú SAC acordó proveer energía eléctrica a Electro Oriente SA. ( Moción de Orden del Día 10992)": "Comisión Reserva Fría de Iquitos",
    "Comisión investigadora multipartidaria respecto a las presuntas irregularidades en los proyectos de inversión, adquisiciones de bienes y servicios, otras actividades desarrolladas y todo lo relacionado a la morbi-mortalidad de pacientes Covid-19, en el ámbito de las siguientes entidades públicas: 1. Gobierno Regional de Moquegua y dependencias y 2. red asistencial EsSalud Moquegua y dependencias; y, otorgarle un plazo de 45 días calendario, a partir de su instalación, para que desarrolle sus actividades correspondientes a las investigaciones y las acciones realizadas en contra de los que resulten responsables, debiendo cumplir a cabalidad con su labor encomendada. (Moción 13988)": "Comisión COVID-19 Moquegua",
    "Comisión Especial Multipartidaria conmemorativa del bicentenario de la independencia del Perú para articular, formular y promover investigaciones,  publicaciones; promover la restauración y conservación del patrimonio cultural de Junín y Ayacucho; identificar y preservar los documentos oficiales  sobre la independencia, entre otras actividades relativas a los doscientos años de la institucionalidad de nuestra independencia nacional, del Congreso  Constituyente de la República, las batallas de Junín y Ayacucho; así como de las capitulaciones de Ayacucho y del Callao. La comisión tendría  vigencia de 5 años del periodo parlamentario. (Moción de Orden del día 440)": "Comisión Bicentenario",
    "Comisión Especial Multipartidaria de Monitoreo, Fiscalización y Control del Programa Hambre Cero, durante el periodo legislativo 2021-2026,  encargada del seguimiento del programa Hambre Cero y de formular iniciativas legislativas que contribuyan a garantizar el derecho a la alimentación  adecuada a la población peruana y la lucha contra la pobreza. Creada por acuerdo del Pleno el 11 de marzo de 2021. (Moción de orden del día 120)": "Comisión Hambre Cero",
    "Comisión Especial de Alto Nivel Multipartidaria, encargada de estudiar y presentar una propuesta de reforma integral del Sistema de Administración de  Justicia en el Perú, en el plazo máximo de 90 calendarios. La comisión estará integrada de forma proporcional por cada grupo parlamentario, así como  por dos representantes de los congresistas no agrupados. (Moción de Orden del Día 15003)": "Comisión Reforma del Sistema de Justicia",
    "Comisión Especial Multipartidaria Conmemorativa del Bicentenario de la Independencia": "Comisión Bicentenario",
    "Comisión Investigadora Multipartidaria que investigue la atención a los niños y las familias afectadas con el exceso de plomo en sangre y demás metales tóxicos en zonas mineras de Pasco y el Perú. La comisión investigaría el cumplimiento de los compromisos y las responsabilidades por parte del Poder Ejecutivo y de las empresas privadas, en relación a la atención de la salud de los afectados por las consecuencias de la actividad minera y metalúrgica. 180 días hábiles. (Moción de Orden del Día 481)": "Comisión Metales Tóxicos en Pasco",
    "Comisión de Investigación de carácter multipartidaria, respecto a la Ejecución del Presupuesto Público y de las Transferencias del Poder Ejecutivo a los programas sociales, organismos públicos descentralizados y organismos supervisores del país. La comisión investigadora ejerce las atribuciones  establecidas en el artículo 88 del Reglamento del Congreso, por un plazo de 250 días hábiles, a efectos de determinar las posibles responsabilidades penales penales, civiles, administrativas o constitucionales a que tenga lugar las autoridades, funcionarios y servidores públicos involucrados (Mociones de Orden del Día (12845)": "Comisión Ejecución del Presupuesto Público",
    "Comisión especial de seguimiento a la organización de los Juegos Panamericanos y Parapanamericanos Lima 2027, hasta el término del actual  periodo parlamentario. (Moción 10683)": "Comisión Lima 2027",
    "Comisión Especial de Seguimiento a Emergencias y Gestión de Riesgo de Desastres, con el objeto de facilitar las acciones del Ejecutivo dentro de las  atribuciones del Congreso de la República para lograr atención inmediata de las medidas dictadas; apoyar su ejecución, investigar y fiscalizar el uso  de los recursos de manera eficiente, asignados a las actividades para el cumplimiento del Plan de Acción-Vigilancia, contención y atención del Covid\x0219 en el Perú hasta el final del periodo parlamentario. (Moción de Orden del día 114)": "Comisión Emergencias y Gestión de Riesgos",
    "Comisión Especial Multipartidaria, encargada de realizar trabajo en conjunto con el Ministerio del Interior y las entidades del Estado peruano  responsables de los objetivos prioritarios y lineamientos en la lucha frontal contra el terrorismo y crimen organizado trasnacional, por el plazo de 180  días.(Moción de Orden del Día 16090)": "Comisión Terrorismo y Crimen Organizado",
    "Grupo de Trabajo de seguimiento de la demanda presentada por el Perú ante la Corte Internacional de Justicia con sede en La Haya por el diferendo marítimo con Chile": "Grupo de Trabajo Diferendo Marítimo Perú-Chile",
    "Subcomisión de Control Político": "Subcomisión de Control Político",
    "Comisión Especial Multipartidaria, encargada del Ordenamiento Legislativo CEMOL (Moción de Orden del Día 206)": "Comisión CEMOL",
    "Comisión especial multipartidaria encargada de proponer, ante el Pleno, a los candidatos para ocupar los tres puestos que le corresponde al Congreso de la República en el Directorio del Banco Central de Reserva (BCR) (081)": "Comisión Directorio del BCR",
    "Comisión investigadora multipartidaria para que en el plazo de 30 días calendario se encargue de investigar las presuntas irregularidades y posibles ilícitos cometidos desde el 28 de julio hasta el 1 de agosto por parte de altos funcionarios y demás personas que resulten involucradas integrantes del gobierno del presidente Pedro Castillo determinándose las responsabilidades a que hubiese lugar por el plazo de 30 días calendarios (Moción de Orden del día 010)": "Comisión Primeros Días del Gobierno de Pedro Castillo",
    "Comisión investigadora multipartidaria, respecto al cobro del concepto de “cargo fijo” en los recibos del servicio de electricidad en el periodo de marzo, abril, mayo y junio de 2020, durante el gobierno del expresidente Martín Vizcarra Cornejo, y otorgarle las facultades de control político, en la modalidad de investigación, fiscalización y control, hasta por el lapso de 120 días hábiles, para determinar las posibles responsabilidades penales, civiles, administrativas o constitucionales de los servidores y funcionarios públicos de todos los sectores públicos y públicos privados del Poder Ejecutivo involucrados. (Moción de orden del día 27)": "Comisión Cargo Fijo de Electricidad",
    "Comisión Especial Multipartidaria de Seguimiento al Proceso de reconstrucción en las Zonas afectadas por el Fenómeno de El Niño Costero, con el objeto de continuar con los encargos que recibió la comisión especial multipartidaria constituida con el mismo fin mediante acuerdo del Pleno del 22 de junio de 2017. La reactivación comprende el periodo parlamentario 2021-2022. (Moción de Orden del día 23": "Comisión Reconstrucción del Niño Costero",
    "Comisión Especial Multipartidaria encargada del seguimiento, coordinación y formulación de propuestas en torno a mejorar la situación de los  peruanos residentes en el extranjero, durante el Período Parlamentario 2021-2026, con la finalidad de contribuir con los alcances pertinentes en  materia legislativa, monitoreo y control político relacionadas a las acciones desplegadas por el Poder Ejecutivo. (Moción de Orden del Día 15875)": "Comisión Peruanos en el Exterior",
    "Comisión encargada del seguimiento, coordinación y formulación de propuestas en materia de mitigación de los efectos del cambio climático, durante  el Periodo Parlamentario 2021-2026, a efectos de contribuir con los esfuerzos en materia legislativa, de seguimiento y de control político a las acciones  desarrolladas por el Poder Ejecutivo. (Moción de Orden del día 60)": "Comisión Cambio Climático",
    "Subcomisión de Acusaciones Constitucionales": "Subcomisión de Acusaciones Constitucionales",
    "Comisión Especial de Selección de Candidata o Candidato Apto para la Elección de Magistrado del Tribunal Constitucional del Congreso de la República": "Comisión Elección del Tribunal Constitucional",
    "Comisión investigadora del proceso de elecciones generales de 2021, de acuerdo a las reglas contenidas en el artículo 88 del Reglamento del Congreso, que se encargará de investigar los presuntos actos de corrupción y cualquier otro tipo de delitos que involucren a funcionarios o servidores públicos; así como a cualquier persona natural que resulte responsable de haber atentado contra el orden electoral y la voluntad popular; y proponer al Congreso de la República las modificaciones a la legislación electoral destinadas a determinar los vacíos legales y el posible aprovechamiento de estos vacíos, que habrían sido usados para cometer las presuntas irregularidades. 120 días hábiles (Moción de Orden del Día 028)": "Comisión Elecciones 2021",
    "Comisión Especial de Seguimiento Parlamentario de la Alianza del Pacífico del Congreso del Perú 2020-2021": "Comisión Alianza del Pacífico",
    "Comisión Especial de estudio encargada de realizar investigaciones, estudios legales y gestionar los proyectos normativos necesarios que permitan  proponer soluciones para que los sectores populares de peruanos emprendedores puedan generar capital (en adelante denominada Comisión Capital  Perú) durante el periodo parlamentario 2021-2026. (Moción de Orden del día 501)": "Comisión Capital Perú",
    "Comisión Especial Multipartidaria Encargada de Elaborar un Nuevo Código Penal actualizado recogiendo el trabajo existente de las comisiones  anteriores (P.L 6962). (Ley 32310)": "Comisión Nuevo Código Penal",
    "Comisión especial multipartidaria de seguimiento, coordinación, monitoreo y fiscalización sobre los avances de los resultados en la prevención y  control del cáncer. La comisión presentaría un informe anual el último día del mes de febrero de cada año. (Moción de Orden del Día 2991": "Comisión Prevención y Control del Cáncer",
    "Comisión Especial de Selección de Candidatos aptos para la elección de Magistrados del Tribunal Constitucional": "Comisión Elección del Tribunal Constitucional",
    "Comisón Especial Multipartidaria de Monitoreo y Seguimiento del Control Concurrente y Elaborar Propuestas Normativas para una mejora en la  gestión del Sistema Presupuestario en el marco del Plan Nacional de Integridad y Lucha contra la Corrupción. (Moción de Orden del Día 7305": "Comisión Control Concurrente",
    "Comisión investigadora multipartidaria encargada de investigar por el plazo de 15 días calendario, el presunto favorecimiento en la aplicación de vacunas contra el Covid-19, respecto al ex Presidente vacado Martín Vizcarra, su familia, ex ministros y/o ministros de Estado, altos funcionarios públicos y demás personas que resulten involucradas, el cual habría ocurrido en el período comprendido entre agosto de 2020 hasta la actualidad, y se determine las responsabilidades que hubiere lugar": "Comisión Vacunagate",
    "Subcomisión encargada de evaluar la propuesta de la señora presidenta de la República para el cargo de contralor general de la República, y que tendrá como plazo hasta el 22 de julio 2024 para emitir su informe.": "Subcomisión del Contralor General",
    "Comisión investigadora sobre presunto incumplimiento de medidas sanitarias y de protección a la ciudadanía frente al Covid-19, en mercados y servicios de transporte público de pasajeros de Lima y Callao. (45 días hábiles)": "Comisión Mercados y Transporte COVID-19",
    "Comisión de Ética Parlamentaria": "Comisión de Ética Parlamentaria",
    "Comisión especial de estudio, para el fortalecimiento e implementación de la Ley N° 32065 con el objetivo de asegurar el cumplimiento de las medidas  establecidas para garantizar el acceso universal al agua potable, así como de promover el fortalecimiento institucional y técnico necesario para la  adecuada implementación de las intervenciones en las zonas urbanas y rurales del país. La comisión tendría un plazo de vigencia hasta el final del  periodo legislativo 2024-2025 (Moción de Orden del Día 13110": "Comisión Acceso Universal al Agua Potable",
    "Comisión investigadora multipartidaria de monitoreo, fiscalización y control del ministerio de Educación, SUNEDU, SINEACE, CONCYTEC y demás órganos adscritos al sector educación, así como de las demás instituciones vinculadas directa e indirectamente; encargada de: fiscalizar, supervisar y monitorear la formulación y cumplimiento de los programas, planes, políticas y otras acciones otorgadas por el poder ejecutivo destinadas a la mejora de la calidad educativa y contribuir a optimización de la misma como parte de los objetivos del estado, se constituye para todo el periodo parlamentario del años 2023-2024, pudiendo ser ampliado. (Moción de Orden del día 7535)": "Comisión Sector Educación",
    "Comisión investigadora multipartidaria, para que en un plazo de 120 días útiles, se investigue desde el año 2018 al presente, todas las licitaciones públicas y contratos de obras convocadas por el Ministerio de Transportes y Comunicaciones, Provías Nacional, Provías Descentralizado, gobiernos regionales y gobiernos locales, así como el estado de ejecución de las obras contratadas, el análisis de la selección que debe contemplar la elaboración de los términos de referencia, la formación de comisión de selección , la actuación de la OSCE, y a los funcionarios y exfuncionarios del Poder Ejecutivo y sus relaciones con postores y contratistas de empresas chinas, para que en el informe respectivo se  determinen las presuntas irregularidades y las responsabilidades a que hubiera lugar. (Moción de Orden del Día 2151)": "Comisión Empresas Chinas",
    "Comisión Especial de Seguimiento Parlamentario al Acuerdo de la Alianza del Pacífico, para el periodo Parlamentario 2021- 2026, destinada a  fortalecer la cooperación, impulsar la inversión, el empleo y el crecimiento de las pymes, a través de la generación de cadenas de valor regionales  integradas al Asia Pacífico (Moción de orden del día 149)": "Comisión Alianza del Pacífico",
    "Comisión investigadora multipartidaria, por un plazo de 90 días calendario, que determine las presuntas responsabilidades penales y políticas de las graves afectaciones a los derechos humanos, tales como la vida y la integridad física, en contra de ciudadanos y agentes del orden ocurridas desde el 7 de diciembre de 2022.  (Moción de orden del día 5039": "Comisión Muertes en Protestas de 2022-2023",
    "Comisión Multipartidaria Encargada de Elaborar una Iniciativa Legislativa sobre una nueva Ley General del Régimen Agrario. Periodo Legislativo 2020-2021": "Comisión Nueva Ley Agraria",
    'Comisión Especial Multipartidaria a favor del Proyecto Especial Chinecas"", para que conjuntamente con el Ejecutivo, estudie, monitoree, proponga y  promueva el cumplimiento de las políticas públicas, planes, programas, proyectos, estrategias, así como que impulse el desarrollo de las etapas y  actividades del Proyecto Especial Chinecas, en la región de Ancash, recomendando y tomando acciones conforme a sus funciones parlamentarias.  (Moción de Orden del Día 2965)': "Comisión Chinecas",
    "Comisión investigadora multipartidaria encargada de investigar el atentado en Vizcatán del Ene el 23 de mayo de 2021, a fin de esclarecer los hechos y determinar las responsabilidades que correspondan. 120 días hábiles. (Moción de Orden del Día 005)": "Comisión Vizcatán del Ene",
    "Comisión especial multipartidaria de seguimiento, coordinación y monitoreo sobre los avances de los resultados de la lucha contra la trata de personas y las medidas que se vienen adoptando para el cumplimiento de las metas y objetivos establecidos en la actual Política Nacional frente a la trata de personas y sus formas de explotación al 2030; asimismo, la comisión estará conformada por un integrante de cada grupo parlamentario, respetando los principios de pluralidad y proporcionalidad, y presentará un informe al final del periodo parlamentario. (Moción de Orden del Día 19370)": "Comisión Trata de Personas",
    "Comisión investigadora multipartidaria encargada de investigar los hechos ocurridos en el distrito de Chancay, especialmente en la zona de Peralvillo, para revisar los permisos de las construcciones de la obra Megapuerto de Chancay, realizada por la empresa Cosco Shipping Ports; asimismo, revisar los expedientes técnicos de dichas construcciones y evaluar los daños ocurridos a la colectividad de Chancay y especialmente en la zona de Peralvillo y su impacto ambiental. La comisión investigadora tendría un plazo 60 días útiles, a partir de su instalación. (Moción de Orden del día 6581)": "Comisión Megapuerto de Chancay",
    "Comisión especial de seguimiento a emergencias y Gestión de Riesgos de Desastres": "Comisión Emergencias y Gestión de Riesgos",
    "Comisión Especial encargada de la elección de candidatas y candidatos aptos para la elección de magistrados del Tribunal Constitucional, conforme a los establecido en el artículo 201 de la Constitución Política y el artículo 6 del reglamento del Congreso. El proceso de selección de magistrados del Tribunal Constitucional se debe desarrollar conforme a los principios de meritocracia y transparencia. (Moción de Orden del día 017)": "Comisión Elección del Tribunal Constitucional",
    "Comisión Especial de Seguimiento de la Incorporación del Perú a la Organización para la Cooperación y el Desarrollo Económico (CESIP  OCDE).(Moción de Orden del día 56)": "Comisión OCDE",
    "Comisión Especial Multipartidaria Pro-Inversión encargada de coadyuvar a fomentar la inversión pública y privada, nacional y extranjera; así como  promover la reactivación de los proyectos de inversión pública paralizados, a nivel nacional. La comisión tendría vigencia hasta el 15 de julio de 2022.  (Moción de Orden del día 502)": "Comisión Pro-Inversión",
    "Comisión de Investigación Multipartidaria orientada a: 1) Investigar e identificar las supuestas irregularidades en el proceso de contratación y construcción del nuevo Hospital Regional Manuel Núñez Butrón, Hospital Materno Infantil de Juliaca, de Ilave y otros. 2) Ejecución del proyecto Drenaje Pluvial de la Ciudad de Juliaca, en un plazo de 50 días calendarios determinar las responsabilidades políticas, administrativas civiles y/o penales de los funcionarios encargados del proceso de contratación y construcción de la infraestructura hospitalaria mencionada. 3) Proponer y promover ante Poder Ejecutivo, mejoras para la solución inmediata de las situaciones irregulares descritas más aún si consideramos que en la actual crisis sanitaria afectan directamente al poblador puneño. Moción 14383)": "Comisión Hospitales y Drenaje de Puno",
    "Subcomisión encargada de evaluar la propuesta del Poder Ejecutivo para el cargo de Contralor General de la República": "Subcomisión del Contralor General",
    "Comisión Especial Multipartidaria de Monitoreo y Seguimiento del Control Concurrente y Elaborar Propuestas Normativas para una mejora en la gestión del Sistema Presupuestario en el marco del Plan Nacional de Integridad y Lucha contra la Corrupción, por el plazo de un año desde su instalación. La Comisión estará conformada por un integrante de cada grupo parlamentario. (Moción de Orden del Día 7305": "Comisión Control Concurrente",
    "COMISIÓN INVESTIGADORA SOBRE LOS POSIBLES EFECTOS POSITIVOS O NEGATIVOS DEL DIÓXIDO DE CLORO EN SERES VIVOS, ENTRE OTROS PUNTOS (MOCIÓN 11833)": "Comisión Dióxido de Cloro",
    "Comisión especial encargada de seleccionar a los  candidatos a Defensor del Pueblo": "Comisión Defensor del Pueblo",
    "Comisión especial de seguimiento del proceso de creación e implementación, y del funcionamiento de la Autoridad Nacional de Infraestructura (ANIN),  hasta el término del actual periodo parlamentario. (Moción de Orden del Día 7515": "Comisión ANIN",
    "Comisión Especial Multipartidaria de Seguridad Ciudadana, encargada de Fiscalizar, supervisar y monitorear la formulación y el cumplimiento de los  programas, planes, políticas, proyectos y otras acciones otorgadas por el Poder Ejecutivo, destinadas a la reducción y eliminación de la inseguridad  ciudadana. Prevenir, reducir y trabajar en la erradicación de la violencia y delincuencia organizada en nuestro país. Evaluar la legislación nacional en  materia de seguridad ciudadana. Evaluar y fortalecer el sistema migratorio en nuestro país. (Moción de Orden del día 044)": "Comisión Seguridad Ciudadana",
    "Exhortar a los 130 Congresistas de la República, en aras de la transparencia y objetividad pública se realicen un Test de Titulación de Anticuerpos Neutralizantes Post Vacuna, a fin de descartar que algún parlamentario se haya vacunado con el lote de vacunas proporcionadas por el laboratorio Sinopharm, para efectos de ensayo clínicos y que son materia de reproche público. Los congresistas asumirán el costo del Test de Titulación de Anticuerpos Neutralizantes Post  Vacuna.": "Test de Anticuerpos a Congresistas",
    "Comisión Especial Investigadora Multipartidaria Encargada de Investigar la Presunta Comisión de Ilícitos en el Sector de la Construcción": "Comisión Club de la Construcción",
    "Comisión  Especial de Seguimiento a la Reconstrucción en  las Zonas Afectadas por el Fenómeno del Niño": "Comisión Reconstrucción del Niño Costero",
    "Comisión especial multipartidaria denominada Comisión Especial Multipartidaria Pro-Inversión encargada de coadyuvar a fomentar la inversión pública y privada, nacional y extranjera; así como promover la reactivación de los proyectos de inversión pública paralizados, a nivel nacional. La comisión tendría vigencia hasta el 15 de julio de 2022. (Moción de Orden del día 502": "Comisión Pro-Inversión",
}
